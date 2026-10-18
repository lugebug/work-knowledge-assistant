"""Synthetic connector tests: no real process, account, disk DB or message access."""
from pathlib import Path
import hashlib
import struct
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))


class ReadOnlyBoundaries(unittest.TestCase):
    def test_only_selected_conversation_and_no_write_or_other_table_access(self):
        from raw_wechat import apsw, query_conversation
        connection = apsw.Connection(':memory:')
        try:
            identity = 'demo_work_chat'
            table = 'Msg_' + hashlib.md5(identity.encode()).hexdigest()
            connection.execute('CREATE TABLE Name2Id(user_name TEXT)')
            connection.execute("INSERT INTO Name2Id VALUES('demo_sender')")
            connection.execute(f'CREATE TABLE {table}(local_id,server_id,local_type,real_sender_id,create_time,message_content,compress_content,packed_info_data)')
            for local_id, typ, body in [(1, 1, '工作文字'), (2, 3, 'image metadata'), (3, 43, 'video metadata'), (4, 34, 'voice metadata')]:
                connection.execute(f'INSERT INTO {table} VALUES(?,?,?,1,1000,?,NULL,NULL)', (local_id, local_id + 100, typ, body))
            connection.execute('CREATE TABLE Msg_private(message_content)')
            connection.execute("INSERT INTO Msg_private VALUES('MUST_NOT_READ')")
            rows, queries = query_conversation(connection, identity, 0, 2000, text_only=True)
            self.assertEqual([row['body'] for row in rows], ['工作文字'])
            self.assertEqual([query['table'] for query in queries], [table])
            self.assertEqual(queries[0]['voice_pending'], 1)
            with self.assertRaises(apsw.AuthError): list(connection.execute('SELECT * FROM Msg_private'))
            with self.assertRaises(apsw.AuthError): connection.execute(f'DELETE FROM {table}')
        finally:
            connection.close()

    def test_only_committed_wal_frames_are_visible_and_stale_tail_is_ignored(self):
        from raw_wechat import checksum, committed_wal
        salt = b'12345678'
        header = struct.pack('>IIII', 0x377f0682, 3007000, 4096, 0) + salt
        state = checksum(header, '<')
        data = header + struct.pack('>II', *state)
        for pgno, size, body in [(2, 2, b'A' * 4096), (2, 0, b'B' * 4096)]:
            first = struct.pack('>II', pgno, size)
            state = checksum(first + body, '<', state)
            data += first + salt + struct.pack('>II', *state) + body
        overlay, size, receipt = committed_wal(data)
        self.assertEqual(overlay[2], b'A' * 4096)
        self.assertEqual(size, 2)
        self.assertEqual(receipt['uncommitted_tail'], 1)
        stale = bytearray(data)
        stale[40:48] = b'87654321'
        self.assertEqual(committed_wal(bytes(stale))[0], {})

    def test_wechat_helper_matches_reviewed_hash(self):
        from raw_wechat import HELPER_HASH, HELPER
        self.assertEqual(hashlib.sha256(HELPER.read_bytes()).hexdigest(), HELPER_HASH)


if __name__ == '__main__':
    unittest.main(verbosity=2)
