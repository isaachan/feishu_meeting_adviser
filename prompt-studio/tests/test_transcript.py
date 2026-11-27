import unittest

from prompt_studio.transcript import (Transcript, TranscriptError, Turn,
                                      parse_text, render_text, scene_json)

SAMPLE = """会参谋 · 文字记录
会话 s-123 · 会议 123456789 · 周会
导出时间 2026/11/26 16:00:00 · 共 4 条

[2026/11/26 16:02:03] 本机: 我们过一下预算
[时间未知] LLM: 预算结论是……
[2026/11/26 16:02:10] 未识别: 我补充一点
续行内容
"""


class ParseTests(unittest.TestCase):
    def test_header_and_turns(self):
        transcript = parse_text(SAMPLE)
        self.assertEqual(transcript.session_id, 's-123')
        self.assertEqual(transcript.meeting_no, '123456789')
        self.assertEqual(transcript.title, '周会')
        self.assertEqual(len(transcript.turns), 3)
        self.assertEqual([turn.kind for turn in transcript.turns],
                         ['human', 'llm', 'human'])
        self.assertEqual(transcript.turns[0].speaker, '本机')
        self.assertEqual(transcript.turns[0].time_text, '2026/11/26 16:02:03')
        self.assertIsNone(transcript.turns[1].time_text)
        self.assertIsNone(transcript.turns[2].speaker)
        self.assertEqual(transcript.turns[2].text, '我补充一点\n续行内容')
        self.assertEqual(len(transcript.human_turns), 2)

    def test_colon_in_text_is_kept(self):
        transcript = parse_text('[2026/11/26 16:02:03] 本机: 结论: 先做 A\n')
        self.assertEqual(transcript.turns[0].text, '结论: 先做 A')

    def test_headerless(self):
        transcript = parse_text('[时间未知] 张三: 你好\n')
        self.assertIsNone(transcript.session_id)
        self.assertEqual(transcript.turns[0].speaker, '张三')
        self.assertIsNone(transcript.turns[0].time_text)

    def test_empty_human_turn_rejected(self):
        with self.assertRaises(TranscriptError):
            parse_text('[时间未知] 张三: \n')

    def test_validate_needs_human_turn(self):
        with self.assertRaises(TranscriptError):
            Transcript(turns=[Turn(kind='llm', speaker='LLM', text='x')]).validate()

    def test_validate_rejects_blank_turn(self):
        with self.assertRaises(TranscriptError):
            Transcript(turns=[Turn(kind='human', speaker=None, text=' ')]).validate()


class SceneTests(unittest.TestCase):
    def test_roundtrip_dict(self):
        transcript = parse_text(SAMPLE)
        transcript.description = '讨论预算'
        transcript.documents = [{'kind': 'doc', 'title': '预算', 'text': '500 万'}]
        clone = Transcript.from_dict(transcript.to_dict())
        self.assertEqual(clone.to_dict(), transcript.to_dict())

    def test_scene_json_is_unicode(self):
        scene_json(parse_text(SAMPLE))
        self.assertIn('我们过一下预算', scene_json(parse_text(SAMPLE)))

    def test_render_reparses(self):
        transcript = parse_text(SAMPLE)
        reparsed = parse_text(render_text(transcript))
        self.assertEqual([turn.to_dict() for turn in reparsed.turns],
                         [turn.to_dict() for turn in transcript.turns])

    def test_from_dict_rejects_bad_kind(self):
        with self.assertRaises(TranscriptError):
            Transcript.from_dict({'turns': [{'kind': 'bot', 'text': 'x'}]})

    def test_from_dict_rejects_bad_documents(self):
        with self.assertRaises(TranscriptError):
            Transcript.from_dict({'turns': [], 'documents': 'x'})


if __name__ == '__main__':
    unittest.main()
