from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

from feishu_meeting_adviser.client import ApiError, Credentials, ErrorKind
from feishu_meeting_adviser.context_probe import (
    ContextProbeClient, ProbeInputError, description_references, document_reference,
    probe, save_report,
)
from feishu_meeting_adviser.storage import StorageError


DOC = 'https://synthetic.feishu.cn/docx/DocToken123'
WIKI = 'https://synthetic.feishu.cn/wiki/WikiToken123'


class FakeClient:
    def __init__(self):
        self.calls = []
        self.event_value = {'event_id': 'event_0', 'summary': '测试日程',
                            'description': '', 'attachments': []}
        self.node_value = {'obj_type': 'docx', 'obj_token': 'ResolvedDoc123', 'title': '测试节点'}

    def event(self, calendar_id, event_id):
        self.calls.append(('event', calendar_id, event_id))
        if isinstance(self.event_value, Exception):
            raise self.event_value
        return self.event_value

    def node(self, token):
        self.calls.append(('node', token))
        return self.node_value

    def document(self, token):
        self.calls.append(('document', token))
        return {'title': '测试文档', 'revision_id': 3}

    def document_text(self, token):
        self.calls.append(('body', token))
        return '测试正文。' * 600


class Response(BytesIO):
    def __init__(self, body):
        super().__init__(json.dumps(body).encode())
        self.status, self.headers = 200, {}


class ContextProbeTests(unittest.TestCase):
    def test_read_transport_encodes_identifiers_and_forbids_bot_mutations(self):
        requests = []
        responses = iter([
            Response({'code': 0, 'tenant_access_token': 'private-token', 'expire': 7200}),
            Response({'code': 0, 'data': {'event': {'event_id': 'event_0'}}}),
        ])
        def opener(req, timeout):
            requests.append(req)
            return next(responses)
        client = ContextProbeClient(Credentials('private-app', 'private-secret'),
            opener=opener, wait=lambda seconds: False)
        for action in (lambda: client.join('012345678'), lambda: client.leave('meeting'),
                       lambda: client.authenticated('/open-apis/docx/v1/documents/x', payload={})):
            with self.assertRaises(ValueError):
                action()
        self.assertEqual(requests, [])
        self.assertEqual(client.event('feishu.cn_test@group.calendar.feishu.cn', 'event_0')['event_id'], 'event_0')
        self.assertEqual(requests[-1].method, 'GET')
        self.assertIn('test%40group.calendar.feishu.cn/events/event_0', requests[-1].full_url)
        self.assertNotIn('private-secret', requests[-1].full_url)

    def test_direct_urls_normalize_and_untrusted_or_indirect_links_are_rejected(self):
        self.assertEqual(document_reference(DOC + '?access_token=hidden#heading')['source'], DOC)
        for value in ('https://feishu.cn.evil.example/docx/Token', 'http://my.feishu.cn/wiki/Token',
                      'https://user:password@my.feishu.cn/wiki/Token',
                      'https://my.feishu.cn:443/wiki/Token', 'https://my.feishu.cn/wiki/a/b',
                      'https://vc.feishu.cn/j/012345678'):
            with self.subTest(value=value), self.assertRaises(ProbeInputError):
                document_reference(value)

    def test_html_and_plain_text_references_deduplicate_without_following_nested_links(self):
        text = f'<p>背景：<a href="{WIKI}?from=calendar&amp;share=1">材料</a></p> {DOC}。 {WIKI}'
        self.assertEqual([(item['type'], item['token']) for item in description_references(text)],
                         [('wiki', 'WikiToken123'), ('docx', 'DocToken123')])

    def test_explicit_wiki_read_does_not_claim_event_linkage(self):
        client = FakeClient()
        report = probe(client, document_urls=[WIKI], meeting_no='012345678')
        item = report['documents'][0]
        self.assertEqual(report['event']['status'], 'not_requested')
        self.assertEqual(report['event_linkage'], 'not_verified')
        self.assertEqual(item['origin'], 'explicit_check_only')
        self.assertEqual(item['expected_linkage'], 'not_verified')
        self.assertIn(('body', 'ResolvedDoc123'), client.calls)
        self.assertNotIn(('body', 'WikiToken123'), client.calls)
        self.assertEqual(item['body']['characters'], 3000)
        self.assertEqual(len(item['body']['preview']), 2000)
        self.assertTrue(item['body']['truncated'])

    def test_event_description_association_and_empty_attachments(self):
        client = FakeClient()
        client.event_value.update(description=f'<a href="{DOC}">背景</a>',
            vchat={'meeting_url': 'https://vc.feishu.cn/j/012345678'})
        report = probe(client, calendar_id='calendar', event_id='event_0', document_urls=[DOC], meeting_no='012345678')
        self.assertEqual(report['event']['attachments'], {'status': 'ok', 'count': 0, 'items': [], 'truncated': False})
        self.assertEqual(report['event']['meeting_link_check'], 'match')
        self.assertEqual(report['documents'][0]['expected_linkage'], 'found_in_description')
        self.assertEqual(sum(call[0] == 'body' for call in client.calls), 1)

    def test_real_format_markdown_rich_description_keeps_wiki_reference_missing_from_plain_description(self):
        client = FakeClient()
        client.event_value.update(description='参考资料：合成文档标题',
            description_rich=f'参考资料：[合成文档标题]({WIKI})')
        report = probe(client, calendar_id='calendar', event_id='event_0', document_urls=[WIKI])
        self.assertEqual(report['event']['description_rich']['status'], 'ok')
        self.assertEqual(report['event']['description_document_count'], 1)
        self.assertEqual(report['documents'][0]['origin'], 'event_description')
        self.assertEqual(report['documents'][0]['source_fields'], ['description_rich'])
        self.assertEqual(report['documents'][0]['expected_linkage'], 'found_in_description')
        self.assertIn(('node', 'WikiToken123'), client.calls)
        self.assertIn(('body', 'ResolvedDoc123'), client.calls)
        client.event_value['description'] += ' ' + WIKI
        report = probe(client, calendar_id='calendar', event_id='event_0')
        self.assertEqual(len(report['documents']), 1)
        self.assertEqual(report['documents'][0]['source_fields'], ['description', 'description_rich'])

    def test_calendar_attachment_token_is_never_used_as_a_document_id(self):
        client = FakeClient()
        client.event_value['attachments'] = [{'file_token': 'CalendarMedia123', 'name': '资料.pdf', 'file_size': '123'}]
        report = probe(client, calendar_id='calendar', event_id='event_0', document_urls=[DOC])
        self.assertEqual(report['event_linkage'], 'no_document_reference_found')
        self.assertEqual(report['documents'][0]['expected_linkage'], 'not_found_in_event')
        self.assertEqual(report['event']['attachments']['items'][0]['status'], 'unresolved_calendar_attachment')
        self.assertNotIn(('body', 'CalendarMedia123'), client.calls)

    def test_event_failure_is_distinct_from_an_empty_list_and_document_check_can_continue(self):
        client = FakeClient()
        client.event_value = ApiError(ErrorKind.DENIED, http_status=403, code=191002)
        report = probe(client, calendar_id='calendar', event_id='event_0', document_urls=[DOC])
        self.assertEqual(report['event']['status'], 'failed')
        self.assertEqual(report['event']['code'], 191002)
        self.assertNotIn('attachments', report['event'])
        self.assertEqual(report['documents'][0]['status'], 'ok')
        self.assertEqual(report['documents'][0]['expected_linkage'], 'not_verified')

    def test_non_docx_wiki_is_reported_without_attempting_a_docx_read(self):
        client = FakeClient()
        client.node_value['obj_type'] = 'sheet'
        report = probe(client, document_urls=[WIKI])
        self.assertEqual(report['documents'][0]['status'], 'unsupported')
        self.assertEqual(client.calls, [('node', 'WikiToken123')])

    def test_discovery_is_bounded_and_event_id_mismatch_is_rejected(self):
        client = FakeClient()
        client.event_value['description'] = ' '.join(f'https://synthetic.feishu.cn/docx/Token{n}' for n in range(12))
        report = probe(client, calendar_id='calendar', event_id='event_0')
        self.assertEqual(len(report['documents']), 10)
        self.assertTrue(report['document_limit']['truncated'])
        client.event_value['event_id'] = 'different_event_0'
        report = probe(client, calendar_id='calendar', event_id='event_0')
        self.assertEqual(report['event']['status'], 'failed')
        self.assertEqual(report['documents'], [])

    def test_invalid_binding_is_rejected_before_network_and_saved_report_is_private(self):
        client = FakeClient()
        for kwargs in ({'calendar_id': 'calendar'}, {},
                       {'calendar_id': '../private', 'event_id': 'event_0'},
                       {'document_urls': [DOC], 'meeting_no': 'not-a-number'}):
            with self.assertRaises(ProbeInputError):
                probe(client, **kwargs)
        self.assertEqual(client.calls, [])
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / 'data' / 'context_probe'
            report = probe(client, document_urls=[DOC])
            path = save_report(report, directory)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(json.loads(path.read_text()), report)
            link = directory / 'linked'
            link.symlink_to(directory, target_is_directory=True)
            with self.assertRaises(StorageError):
                save_report(report, link)

    def test_calendar_metadata_pagination_does_not_read_events_implicitly(self):
        requests = []
        responses = iter([Response({'code': 0, 'tenant_access_token': 'private-token', 'expire': 7200}),
            Response({'code': 0, 'data': {'calendar_list': [{'calendar_id': 'calendar',
                'summary': '测试日历', 'role': 'reader', 'type': 'primary'}],
                'has_more': True, 'page_token': 'opaque-next-page'}})])
        def opener(req, timeout):
            requests.append(req)
            return next(responses)
        client = ContextProbeClient(Credentials('synthetic-app', 'private-secret'),
            opener=opener, wait=lambda seconds: False)
        result = client.calendars()
        self.assertTrue(result['has_more'])
        self.assertEqual(result['calendars'][0]['calendar_id'], 'calendar')
        self.assertEqual(result['page_token'], 'opaque-next-page')
        self.assertEqual(len(requests), 2)
        self.assertNotIn('/events', requests[-1].full_url)

    def test_explicit_time_window_finds_only_exact_meeting_links_and_never_saves_binding(self):
        requests = []
        items = [{'event_id': 'event_0', 'summary': '目标',
                  'vchat': {'meeting_url': 'https://vc.feishu.cn/j/012345678'}},
                 {'event_id': 'other_0', 'vchat': {'meeting_url': 'https://evil.example/j/012345678'}},
                 {'event_id': 'query_0', 'vchat': {'meeting_url': 'https://vc.feishu.cn/j/999999999?ref=012345678'}}]
        responses = iter([Response({'code': 0, 'tenant_access_token': 'private-token', 'expire': 7200}),
            Response({'code': 0, 'data': {'items': items, 'has_more': False}})])
        def opener(req, timeout):
            requests.append(req)
            return next(responses)
        client = ContextProbeClient(Credentials('synthetic-app', 'private-secret'),
            opener=opener, wait=lambda seconds: False)
        with self.assertRaises(ProbeInputError):
            client.find_event('calendar', '2026-10-14T15:30:00', '2026-10-14T19:30:00', '012345678')
        self.assertEqual(requests, [])
        result = client.find_event('calendar', '2026-10-14T15:30:00+08:00',
                                   '2026-10-14T19:30:00+08:00', '012345678')
        self.assertEqual([item['event_id'] for item in result['matches']], ['event_0'])
        self.assertFalse(result['binding_saved'])
        self.assertFalse(result['truncated_possible'])
        self.assertIn('/calendars/calendar/events?', requests[-1].full_url)


if __name__ == '__main__':
    unittest.main()
