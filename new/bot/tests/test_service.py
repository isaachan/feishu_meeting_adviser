"""Plan-1.5 acceptance: command parsing (legal/illegal/@mentions/password links)
and full stub-mode command round trips through BotService."""
import unittest

from bot.advisor_client import AdvisorApiError
from bot.commands import CommandError, parse_command, parse_meeting_reference, strip_mentions
from bot.recorder_command import CommandRenderError, render
from bot.service import BotService

ARGS = {'session': 's-3f9c2a', 'event': 'cal_x/evt_y',
        'url': 'ws://127.0.0.1:8400/ingest', 'token': 'st-abcdefghijklmnop123456'}


class MeetingReferenceTests(unittest.TestCase):
    def test_nine_digit_number(self):
        self.assertEqual(parse_meeting_reference(' 012345678 '), ('012345678', ''))

    def test_join_link_with_password(self):
        self.assertEqual(parse_meeting_reference('https://vc.feishu.cn/j/012345678/?pwd=secret'),
                         ('012345678', 'secret'))

    def test_illegal_references(self):
        for bad in ('12345', '1234567890', 'http://vc.feishu.cn/j/012345678',
                    'https://evil.com/j/012345678', 'https://vc.feishu.cn/j/abc',
                    'https://vc.feishu.cn/j/012345678/?pwd=a&pwd=b', '', None,
                    'https://vc.feishu.cn/other/012345678'):
            with self.assertRaises(CommandError, msg=repr(bad)):
                parse_meeting_reference(bad)


class ParseCommandTests(unittest.TestCase):
    def test_join(self):
        command = parse_command('入会 123456789')
        self.assertEqual((command.kind, command.meeting_no), ('join', '123456789'))
        command = parse_command('入会 https://vc.feishu.cn/j/123456789?pwd=x1')
        self.assertEqual((command.meeting_no, command.password), ('123456789', 'x1'))

    def test_end_with_optional_meeting(self):
        self.assertEqual(parse_command('结束会议').kind, 'end')
        self.assertEqual(parse_command('结束会议 123456789').meeting_no, '123456789')

    def test_approve_deny_forms(self):
        self.assertEqual(parse_command('同意 #r12').request_no, 12)
        self.assertEqual(parse_command('同意 #12').request_no, 12)
        self.assertEqual(parse_command('拒绝 #r3').kind, 'deny')
        with self.assertRaises(CommandError):
            parse_command('同意 12')

    def test_revoke_forms(self):
        command = parse_command('撤销订阅 小B 123456789')
        self.assertEqual((command.kind, command.subscriber_name, command.meeting_no),
                         ('revoke', '小B', '123456789'))
        command = parse_command('撤销订阅 张 三 123456789')
        self.assertEqual(command.subscriber_name, '张 三')
        self.assertEqual(parse_command('撤销 #7').request_no, 7)

    def test_status_and_mentions(self):
        self.assertEqual(parse_command('状态').kind, 'status')
        self.assertEqual(parse_command('@会参谋 状态').kind, 'status')
        self.assertEqual(parse_command('@会参谋  入会 123456789').kind, 'join')

    def test_subscribe_intent_from_plain_text(self):
        command = parse_command('你好，我想收到会议 123456789 的信息')
        self.assertEqual((command.kind, command.meeting_no), ('subscribe', '123456789'))
        self.assertIsNone(parse_command('随便聊聊'))
        self.assertIsNone(parse_command('@会参谋 在吗'))


class RenderCommandTests(unittest.TestCase):
    def test_renders_appendix_template(self):
        text = render(ARGS)
        self.assertIn("--session 's-3f9c2a'", text)
        self.assertIn("--url 'ws://127.0.0.1:8400/ingest'", text)
        self.assertNotIn('--mic', text)

    def test_event_accepts_real_feishu_id_shapes(self):
        # real calendar/event ids carry . @ ~ and inner slashes (2026-11-11 probe)
        for event in ('cal_abc/evt_xyz', 'user@feishu.cn/evt.ics-1',
                       'cal.primary/evt_outer/inner~part'):
            text = render(ARGS | {'event': event})
            self.assertIn(f"--event '{event}'", text)

    def test_mic_and_speaker(self):
        text = render(ARGS | {'mic': True})
        self.assertIn('--mic', text)
        self.assertNotIn('--mic-speaker', text)  # default label omitted
        text = render(ARGS | {'mic': True, 'mic_speaker': '张三'})
        self.assertIn("--mic-speaker '张三'", text)

    def test_rejects_unknown_or_dangerous_values(self):
        with self.assertRaises(CommandRenderError):
            render(ARGS | {'session': 'local:abc'})
        with self.assertRaises(CommandRenderError):
            render(ARGS | {'token': "st-ok'; rm -rf /"})
        with self.assertRaises(CommandRenderError):
            render(ARGS | {'url': 'wss://127.0.0.1:8400/ingest'})
        with self.assertRaises(CommandRenderError):
            render(ARGS | {'extra': 'x'})

    def test_red_line_no_user_credentials_in_text(self):
        known_secrets = {'u-at-user-access-token', 'r-refresh-token-xyz'}
        text = render(ARGS | {'mic': True})
        for secret in known_secrets:
            self.assertNotIn(secret, text)


class FakeTransport:
    def __init__(self):
        self.replies = []
        self.dms = []

    def send_reply(self, message_id, text):
        self.replies.append((message_id, text))
        return f'om-reply-{len(self.replies)}'

    def send_dm(self, open_id, text):
        self.dms.append((open_id, text))
        return f'om-dm-{len(self.dms)}'


class FakeAdvisor:
    def __init__(self):
        self.calls = []
        self.session = {'session_id': 's-abc123', 'meeting_no': '123456789',
                        'title': '周会', 'phase': 'recording', 'existing': False,
                        'recorder_command_args': dict(ARGS)}
        self.error = None
        self.subscriptions = []
        self.pending = []
        self.meetings = {'now': 0, 'meetings': []}

    def create_session(self, owner, meeting_no, *, password='', mic=False, message_id=''):
        self.calls.append(('create_session', owner, meeting_no, message_id))
        if self.error:
            raise self.error
        result = dict(self.session)
        result['recorder_command_args'] = dict(self.session['recorder_command_args'])
        return result

    def list_sessions(self, owner):
        self.calls.append(('list_sessions', owner))
        return {'sessions': [{'session_id': self.session['session_id'],
                              'meeting_no': '123456789', 'title': '周会',
                              'phase': 'recording', 'started_at': 1}]}

    def session_status(self, session_id):
        self.calls.append(('session_status', session_id))
        return {'session_id': session_id, 'phase': 'recording',
                'model_state': 'idle', 'subscriptions': self.subscriptions}

    def end_session(self, session_id, owner, *, message_id=''):
        self.calls.append(('end_session', session_id, owner))
        return {'session_id': session_id, 'phase': 'draining'}

    def create_subscription(self, subscriber, meeting_no, *, message_id=''):
        self.calls.append(('create_subscription', subscriber, meeting_no))
        if self.error:
            raise self.error
        return {'request_no': 7, 'state': 'requested', 'existing': False,
                'owner_open_id': 'ou_A', 'title': '周会'}

    def decide_subscription(self, request_no, owner, decision, *, message_id=''):
        self.calls.append(('decide', request_no, owner, decision))
        return {'request_no': request_no, 'state': 'approved' if decision == 'approve' else 'denied'}

    def revoke_subscription(self, request_no, owner, *, message_id=''):
        self.calls.append(('revoke', request_no, owner))
        return {'request_no': request_no, 'state': 'revoked'}

    def list_meetings(self, owner_open_id):
        self.calls.append(('list_meetings', owner_open_id))
        return self.meetings

    def pending_outbox(self, limit=10):
        self.calls.append(('pending', limit))
        return {'rows': list(self.pending)}

    def outbox_result(self, uuid, status, *, error='', message_id=''):
        self.calls.append(('result', uuid, status))


class BotServiceTests(unittest.TestCase):
    def setUp(self):
        self.transport = FakeTransport()
        self.advisor = FakeAdvisor()
        self.service = BotService(self.advisor, self.transport,
                                  poll_interval=0.05)

    def handle(self, text, *, sender='ou_A', message_id=None, name=''):
        self.service.handle_message(sender, text, message_id or f'om-{abs(hash(text))}',
                                    sender_name=name)
        return self.transport.replies[-1][1] if self.transport.replies else ''

    def test_join_round_trip_renders_command(self):
        reply = self.handle('入会 123456789', message_id='om-1')
        self.assertIn('已入会《周会》', reply)
        self.assertIn("recorder real --session 's-3f9c2a'", reply)
        self.assertNotIn('--mic', reply)
        self.assertEqual(self.advisor.calls[0][:3], ('create_session', 'ou_A', '123456789'))

    def test_join_duplicate_message_ignored(self):
        self.service.handle_message('ou_A', '入会 123456789', 'om-dup')
        count = len(self.transport.replies)
        self.service.handle_message('ou_A', '入会 123456789', 'om-dup')
        self.assertEqual(len(self.transport.replies), count)

    def test_authorization_required_sends_link(self):
        self.advisor.error = AdvisorApiError(
            409, 'authorization_required', '', {'authorize_url': 'https://x/oauth?state=1'})
        reply = self.handle('入会 123456789', message_id='om-2')
        self.assertIn('授权', reply)
        self.assertIn('https://x/oauth?state=1', reply)

    def test_four_rejections_have_reasons(self):
        for code, word in (('meeting_not_found', '未在日历中找到'),
                           ('not_organizer', '组织者'),
                           ('outside_window', '时间窗'),
                           ('auth_unavailable', '稍后再试')):
            self.advisor.error = AdvisorApiError(409, code, '')
            reply = self.handle('入会 123456789', message_id=f'om-{code}')
            self.assertIn(word, reply)

    def test_end_picks_latest_session(self):
        reply = self.handle('结束会议', message_id='om-3')
        self.assertIn('排空', reply)

    def test_subscribe_flow_notifies_owner(self):
        reply = self.handle('我想收到会议 123456789 的信息', sender='ou_B', message_id='om-4')
        self.assertIn('#7', reply)
        self.assertEqual(len(self.transport.dms), 1)
        owner_dm = self.transport.dms[0]
        self.assertEqual(owner_dm[0], 'ou_A')
        self.assertIn('同意 #7', owner_dm[1])

    def test_approve_flow(self):
        self.handle('同意 #7', message_id='om-5')
        self.assertIn('已批准', self.transport.replies[-1][1])

    def test_revoke_by_nickname_via_directory(self):
        self.handle('我想收到会议 123456789 的信息', sender='ou_B', message_id='om-6',
                    name='小B')
        self.advisor.subscriptions = [{'request_no': 7, 'subscriber_open_id': 'ou_B',
                                       'state': 'approved'}]
        reply = self.handle('撤销订阅 小B 123456789', message_id='om-7')
        self.assertIn('已撤销', reply)

    def test_meetings_listing(self):
        self.advisor.meetings = {'now': 0, 'meetings': [
            {'meeting_no': '123456789', 'title': '周会', 'start_ts': 1790000000,
             'end_ts': 1790003600, 'is_organizer': True, 'in_progress': False},
            {'meeting_no': None, 'title': '无链接会', 'start_ts': 1790100000,
             'end_ts': 1790103600, 'is_organizer': False, 'in_progress': False}]}
        reply = self.handle('会议列表', message_id='om-m1')
        self.assertIn('《周会》 会议号 123456789', reply)
        self.assertIn('你是组织者', reply)
        self.assertIn('无会议号', reply)
        self.assertIn('非组织者', reply)

    def test_meetings_parse_forms(self):
        self.assertEqual(parse_command('会议列表').kind, 'meetings')
        self.assertEqual(parse_command('会议').kind, 'meetings')

    def test_approve_without_number_uses_newest_pending(self):
        self.advisor.subscriptions = [
            {'request_no': 3, 'subscriber_open_id': 'ou_C', 'state': 'requested'},
            {'request_no': 9, 'subscriber_open_id': 'ou_B', 'state': 'requested'}]
        reply = self.handle('同意', message_id='om-approve-plain')
        self.assertIn('#9', reply)

    def test_revoke_without_number_needs_unique_approved(self):
        self.advisor.subscriptions = [
            {'request_no': 5, 'subscriber_open_id': 'ou_B', 'state': 'approved'}]
        reply = self.handle('撤销', message_id='om-revoke-plain')
        self.assertIn('已撤销', reply)
        self.advisor.subscriptions = [
            {'request_no': 5, 'subscriber_open_id': 'ou_B', 'state': 'approved'},
            {'request_no': 8, 'subscriber_open_id': 'ou_C', 'state': 'approved'}]
        reply = self.handle('撤销', message_id='om-revoke-two')
        self.assertIn('#5', reply)
        self.assertIn('#8', reply)

    def test_status_lists_sessions(self):
        reply = self.handle('状态', message_id='om-8')
        self.assertIn('《周会》', reply)

    def test_help_on_unknown(self):
        self.assertIn('可用指令', self.handle('随便聊聊', message_id='om-9'))

    def test_outbox_poll_delivers_and_acks(self):
        self.advisor.pending = [{'uuid': 'u-1', 'kind': 'reply', 'channel': 'im',
                                 'recipient_open_id': 'ou_A', 'session_id': 's-abc123',
                                 'payload': {'text': '回答内容'}, 'created_at': 1,
                                 'attempts': 0}]
        self.service.poll_once()
        self.assertEqual(self.transport.dms[-1], ('ou_A', '回答内容'))
        self.assertIn(('result', 'u-1', 'sent'), self.advisor.calls)

    def test_outbox_poll_uncertain_on_failure(self):
        class BrokenTransport(FakeTransport):
            def send_dm(self, open_id, text):
                raise TimeoutError('feishu timeout')

        self.service.transport = BrokenTransport()
        self.advisor.pending = [{'uuid': 'u-2', 'kind': 'reply', 'channel': 'im',
                                 'recipient_open_id': 'ou_A', 'session_id': 's',
                                 'payload': {'text': 'x'}, 'created_at': 1, 'attempts': 0}]
        self.service.poll_once()
        self.assertIn(('result', 'u-2', 'uncertain'), self.advisor.calls)


if __name__ == '__main__':
    unittest.main()
