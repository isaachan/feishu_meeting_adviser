"""reply-md-v1 rendering: Feishu card Markdown with a plain-text safety net."""
import unittest

from bot.render import MAX_MARKDOWN_BYTES, OutboundMessage, render, sanitize_markdown


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


if __name__ == '__main__':
    unittest.main()
