"""Single-character chat acknowledgements without relaxing source boundaries."""
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from work_sources import resolve_reference


class ShortRawReferences(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE raw_sources(id TEXT, name TEXT, state TEXT, kind TEXT);
            CREATE TABLE raw_versions(id TEXT, source_id TEXT, object_kind TEXT, payload TEXT);
        ''')
        self.db.execute('INSERT INTO raw_sources VALUES(?,?,?,?)', ('source', '工作聊天', 'approved', 'chat'))
        self.set_body('好')

    def tearDown(self):
        self.db.close()

    def set_body(self, body, kind='message'):
        self.db.execute('DELETE FROM raw_versions')
        self.db.execute('INSERT INTO raw_versions VALUES(?,?,?,?)',
                        ('raw:test', 'source', kind, json.dumps({'body': body, 'title': '工作聊天'})))

    def resolve(self, quote):
        return resolve_reference(self.db, {'type': 'raw', 'id': 'raw:test', 'quote': quote})

    def test_entire_single_character_chat(self):
        self.assertEqual(self.resolve('好')['quote'], '好')

    def test_character_from_longer_message_rejected(self):
        self.set_body('好的')
        with self.assertRaisesRegex(ValueError, '完整'):
            self.resolve('好')

    def test_single_character_document_rejected(self):
        self.db.execute("UPDATE raw_sources SET kind='document'")
        self.set_body('好', 'document')
        with self.assertRaisesRegex(ValueError, '聊天'):
            self.resolve('好')

    def test_unapproved_source_checked_before_body(self):
        self.db.execute("UPDATE raw_sources SET state='paused'")
        self.db.execute("UPDATE raw_versions SET payload='not-json'")
        with self.assertRaisesRegex(ValueError, '未批准'):
            self.resolve('好')

    def test_empty_or_whitespace_rejected(self):
        for body in ('', ' '):
            self.set_body(body)
            with self.assertRaises(ValueError):
                self.resolve(body)

    def test_longer_reference_still_validates_exact_quote(self):
        self.set_body('稍后核对原工作')
        self.assertEqual(self.resolve('核对原工作')['quote'], '核对原工作')
        with self.assertRaisesRegex(ValueError, '不一致'):
            self.resolve('工作已完成')


if __name__ == '__main__':
    unittest.main()
