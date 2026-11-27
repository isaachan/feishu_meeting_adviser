import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from prompt_studio.results import (ResultError, read_jobs, read_material,
                                   read_records, read_session)


class ResultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'adviser.sqlite3'
        connection = sqlite3.connect(self.db)
        connection.executescript("""
            CREATE TABLE sessions (
                session_id TEXT PRIMARY KEY, meeting_no TEXT, title TEXT,
                phase TEXT, generation INTEGER, decided_generation INTEGER,
                error TEXT, retry_at REAL, material_state TEXT,
                material_version TEXT);
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY, session_id TEXT, generation INTEGER,
                snapshot TEXT, role_version TEXT, state TEXT, result TEXT,
                created_ns INTEGER, context_version TEXT, timings TEXT);
            CREATE TABLE context (
                session_id TEXT, sentence_key TEXT, body TEXT, suppressed INTEGER,
                generation INTEGER, PRIMARY KEY (session_id,sentence_key));
            CREATE TABLE outbox (
                uuid TEXT PRIMARY KEY, session_id TEXT, kind TEXT, content TEXT,
                evidence TEXT, generation INTEGER, state TEXT, error TEXT,
                created_ns INTEGER, sent_at REAL, context_version TEXT,
                answer_uuid TEXT, channel TEXT, recipient_id TEXT,
                delivery_deadline_at REAL);
            CREATE TABLE session_materials (
                session_id TEXT PRIMARY KEY, body TEXT, loaded_ns INTEGER);
        """)
        connection.execute(
            'INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('s-1', '123456789', '周会', 'running', 2, 1, None, 0.0,
             'ready', 'v1'))
        connection.execute(
            'INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('j-1', 's-1', 1, json.dumps({'captions': [1],
                                          'fresh_input_keys': ['k1'],
                                          'reply_allowed_keys': ['k1']}),
             'rv', 'complete', json.dumps({'control': 'none', 'reply': 'hi'}),
             1, 'v1', json.dumps({'execution_ms': 12})))
        connection.execute(
            'INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)',
            ('j-2', 's-1', 0, json.dumps({'snapshot_hash': 'x'}),
             'rv', 'abandoned', None, 0, 'v1', None))
        connection.execute(
            'INSERT INTO context VALUES (?,?,?,?,?)',
            ('s-1', 'k1', json.dumps({'text': '第一句', 'speaker_name': '本机',
                                      'end_time': 1762800000000}), 0, 1))
        connection.execute(
            'INSERT INTO outbox VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            ('o-1', 's-1', 'reply', '回答', json.dumps(['k1']), 1, 'sent',
             None, 1762800001000000000, None, 'v1', 'a-1', 'local', None, None))
        connection.execute(
            'INSERT INTO session_materials VALUES (?,?,?)',
            ('s-1', json.dumps({'state': 'ready', 'description': '测试',
                                'documents': []}), 0))
        connection.commit()
        connection.close()

    def test_read_session(self):
        state = read_session(self.db, 's-1')
        self.assertEqual(state.generation, 2)
        self.assertEqual(state.decided_generation, 1)
        self.assertIsNone(read_session(self.db, 'missing'))

    def test_read_jobs(self):
        jobs = read_jobs(self.db, 's-1')
        self.assertEqual([job.job_id for job in jobs], ['j-2', 'j-1'])
        complete = jobs[1]
        self.assertEqual(complete.decision, {'control': 'none', 'reply': 'hi'})
        self.assertEqual(complete.timings, {'execution_ms': 12})
        self.assertEqual(complete.snapshot_summary()['fresh_input_keys'], ['k1'])
        self.assertFalse(complete.snapshot_summary()['meeting_context'])

    def test_read_records_orders_human_then_reply(self):
        records = read_records(self.db, 's-1')
        self.assertEqual([record['type'] for record in records], ['human', 'llm'])
        self.assertEqual(records[0]['speaker'], '本机')
        self.assertEqual(records[1]['text'], '回答')

    def test_read_material(self):
        material = read_material(self.db, 's-1')
        self.assertEqual(material['description'], '测试')

    def test_missing_database(self):
        with self.assertRaises(ResultError):
            read_session(Path(self.temp.name) / 'nope.sqlite3', 's-1')


if __name__ == '__main__':
    unittest.main()
