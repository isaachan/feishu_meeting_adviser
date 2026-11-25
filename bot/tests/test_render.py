"""reply-md-v1 rendering: Feishu card Markdown with a plain-text safety net."""
import unittest

from bot.render import (MAX_MARKDOWN_BYTES, OutboundMessage, escape_md,
                        linkify_jira_keys, meeting_header, render,
                        sanitize_markdown)


def card_text(rendered):
    return rendered.content['elements'][0]['text']['content']


class RenderTests(unittest.TestCase):
    def test_plain_text_stays_text(self):
        rendered = render(OutboundMessage('同意 #7', 'text'))
        self.assertEqual(rendered.msg_type, 'text')
        self.assertEqual(rendered.content, {'text': '同意 #7'})

    def test_markdown_becomes_interactive_card(self):
        rendered = render(OutboundMessage('**结论**：先补数据', 'markdown'))
        self.assertEqual(rendered.msg_type, 'interactive')
        self.assertEqual(card_text(rendered), '**结论**：先补数据')

    def test_rich_text_off_falls_back_to_text(self):
        rendered = render(OutboundMessage('**结论**', 'markdown'), rich_text=False)
        self.assertEqual(rendered.msg_type, 'text')

    def test_unsafe_and_unsupported_constructs_are_removed(self):
        cleaned = sanitize_markdown(
            '<at id=all></at>![secret](https://x/y.png)\n'
            '[官网](https://example.com) [坏链](javascript:alert(1))\n'
            '## 小标题\n'
            '\n'
            '```\n'
            'code\n'
            '```')
        self.assertNotIn('<at', cleaned)
        self.assertNotIn('secret', cleaned)
        self.assertIn('[官网](https://example.com)', cleaned)
        self.assertIn('坏链', cleaned)
        self.assertNotIn('javascript:', cleaned)
        self.assertNotIn('##', cleaned)
        self.assertIn('**小标题**', cleaned)
        self.assertNotIn('```', cleaned)

    def test_nested_lists_are_flattened(self):
        cleaned = sanitize_markdown('- 一级\n  - 二级\n  1. 三级')
        self.assertEqual(cleaned, '- 一级\n- 二级\n1. 三级')

    def test_overflow_falls_back_to_text(self):
        text = 'x' * (MAX_MARKDOWN_BYTES + 1)
        rendered = render(OutboundMessage(text, 'markdown'))
        self.assertEqual(rendered.msg_type, 'text')

    def test_empty_markdown_falls_back_to_text(self):
        rendered = render(OutboundMessage('   ', 'markdown'))
        self.assertEqual(rendered.msg_type, 'text')

    def test_outbound_message_is_still_a_string(self):
        message = OutboundMessage('回答', 'markdown')
        self.assertEqual(message, '回答')
        self.assertEqual(message.msg_format, 'markdown')
        self.assertEqual(OutboundMessage('回答', 'bogus').msg_format, 'text')

    def test_escape_md_protects_inline_specials(self):
        self.assertEqual(escape_md('Q3 *预算* [草案] <at>'),
                         'Q3 \\*预算\\* \\[草案\\] \\<at\\>')

    def test_meeting_header_markdown_escapes_title(self):
        self.assertEqual(meeting_header('Q3 *预算* [草案]', '123456789'),
                         '**Q3 \\*预算\\* \\[草案\\]**（会议号 123456789）')

    def test_meeting_header_text_mode_keeps_title(self):
        self.assertEqual(meeting_header('Q3 *预算*', '123456789', markdown=False),
                         'Q3 *预算*（会议号 123456789）')

    def test_meeting_header_blank_title_uses_meeting_no(self):
        self.assertEqual(meeting_header('  ', '123456789'), '会议 123456789')

    def test_meeting_header_collapses_whitespace(self):
        self.assertEqual(meeting_header('周会\n第二行', '123456789', markdown=False),
                         '周会 第二行（会议号 123456789）')


class JiraLinkifyTests(unittest.TestCase):
    base = 'https://jira.example.com'
    keys = ('NT3VFTVDR', 'NT2')

    def link(self, text):
        return linkify_jira_keys(text, jira_url=self.base, project_keys=self.keys)

    def test_bare_key_becomes_browse_link(self):
        self.assertEqual(
            self.link('见 NT3VFTVDR-1234。'),
            '见 [NT3VFTVDR-1234](https://jira.example.com/browse/NT3VFTVDR-1234)。')

    def test_trailing_slash_in_base_is_normalized(self):
        self.assertIn('](https://jira.example.com/browse/NT2-7)',
                      linkify_jira_keys('NT2-7', jira_url='https://jira.example.com/',
                                        project_keys=self.keys))

    def test_non_allowlisted_lookalikes_are_untouched(self):
        for text in ('UTF-8', 'COVID-19', 'NT9-1', 'nt2-1'):
            with self.subTest(text=text):
                self.assertEqual(self.link(text), text)

    def test_multiple_keys_all_linked(self):
        self.assertEqual(
            self.link('NT2-1 和 NT3VFTVDR-9'),
            '[NT2-1](https://jira.example.com/browse/NT2-1) 和 '
            '[NT3VFTVDR-9](https://jira.example.com/browse/NT3VFTVDR-9)')

    def test_existing_link_is_not_double_linked(self):
        text = '[NT2-1](https://jira.example.com/browse/NT2-1)'
        self.assertEqual(self.link(text), text)

    def test_inline_code_is_untouched(self):
        self.assertEqual(self.link('`NT2-1`'), '`NT2-1`')

    def test_bare_url_is_untouched(self):
        text = 'https://jira.example.com/browse/NT2-1'
        self.assertEqual(self.link(text), text)

    def test_no_config_is_a_noop(self):
        self.assertEqual(linkify_jira_keys('NT2-1'), 'NT2-1')
        self.assertEqual(linkify_jira_keys('NT2-1', jira_url=self.base), 'NT2-1')

    def test_render_links_keys_only_in_markdown(self):
        markdown = render(OutboundMessage('跟踪 NT2-1', 'markdown'),
                          jira_url=self.base, jira_project_keys=self.keys)
        self.assertIn('[NT2-1](https://jira.example.com/browse/NT2-1)',
                      card_text(markdown))
        text = render(OutboundMessage('跟踪 NT2-1', 'text'),
                      jira_url=self.base, jira_project_keys=self.keys)
        self.assertEqual(text.content, {'text': '跟踪 NT2-1'})


if __name__ == '__main__':
    unittest.main()
