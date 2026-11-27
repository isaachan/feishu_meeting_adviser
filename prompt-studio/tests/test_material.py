import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from prompt_studio.material import (MaterialPatchError, material_body,
                                    patch_material)


def make_db(path: Path, session_id: str = 's-1') -> None:
    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, material_version TEXT,
            material_state TEXT);
        CREATE TABLE session_materials (
            session_id TEXT PRIMARY KEY, body TEXT, loaded_ns INTEGER);
    """)
    connection.execute('INSERT INTO sessions VALUES (?,?,?)', (session_id, None, None))
    connection.execute('INSERT INTO session_materials VALUES (?,?,?)',
                       (session_id, '{}', 0))
    connection.commit()
    connection.close()


class MaterialBodyTests(unittest.TestCase):
    def test_ready_version_is_stable(self):
        first = material_body('讨论预算', [{'title': '预算', 'text': '500 万'}])
        second = material_body('讨论预算', [{'title': '预算', 'text': '500 万'}])
        third = material_body('讨论预算', [{'title': '预算', 'text': '600 万'}])
        self.assertEqual(first['state'], 'ready')
        self.assertEqual(first['context_version'], second['context_version'])
        self.assertNotEqual(first['context_version'], third['context_version'])
        self.assertEqual(len(first['context_version']), 64)
        self.assertEqual(first['documents'][0]['kind'], 'doc')

    def test_empty_is_disabled(self):
        body = material_body('  ', [{'text': ' '}])
        self.assertEqual(body['state'], 'disabled')
        self.assertIsNone(body['context_version'])

    def test_document_types_are_checked(self):
        with self.assertRaises(MaterialPatchError):
            material_body('x', [{'text': 1}])


class PatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Path(self.temp.name) / 'advisor.sqlite3'
        make_db(self.db)

    def test_patch_updates_session_and_material(self):
        body = patch_material(self.db, 's-1', '讨论预算',
                              [{'title': '预算', 'text': '500 万'}], now=123.5)
        connection = sqlite3.connect(self.db)
        version, state = connection.execute(
            'SELECT material_version,material_state FROM sessions').fetchone()
        stored, loaded_ns = connection.execute(
            'SELECT body,loaded_ns FROM session_materials').fetchone()
        connection.close()
        self.assertEqual(version, body['context_version'])
        self.assertEqual(state, 'ready')
        self.assertEqual(json.loads(stored), body)
        self.assertEqual(loaded_ns, 123_500_000_000)

    def test_unknown_session_rolls_back(self):
        with self.assertRaises(MaterialPatchError):
            patch_material(self.db, 's-missing', 'x')
        connection = sqlite3.connect(self.db)
        self.assertEqual(connection.execute(
            'SELECT material_version FROM sessions').fetchone()[0], None)
        connection.close()

    def test_missing_database(self):
        with self.assertRaises(MaterialPatchError):
            patch_material(Path(self.temp.name) / 'nope.sqlite3', 's-1', 'x')


if __name__ == '__main__':
    unittest.main()
