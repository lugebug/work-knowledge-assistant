"""Explicitly authorized contact/session metadata inventory; never chat bodies."""
from pathlib import Path
import argparse
import datetime as dt
import hashlib
import json
import sys
import uuid

from runtime_config import settings, database_path, require_wechat


def inventory(confirm_directory=False):
    if not confirm_directory:
        raise ValueError('只有本人明确同意查看联系人与群目录后，才可生成清单；未读取目录或聊天。')
    config = require_wechat()
    # Import only after ownership/version checks, before any encrypted page or process read.
    import raw_wechat as reader
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    root = Path(config['database_dir'])
    contact = root / 'contact' / 'contact.db'
    session = root / 'session' / 'session.db'
    if not contact.is_file() or not session.is_file():
        raise ValueError('未找到本人的联系人或会话目录库。')
    targets = [(contact, 'contact', ('username', 'remark', 'nick_name', 'alias', 'local_type', 'delete_flag', 'is_in_chat_room')),
               (session, 'SessionTable', ('username', 'last_timestamp'))]
    snapshots = {}
    receipts = []
    with reader._guard:
        helper = reader.crypto_helper()
        reader.ensure_keys(helper, [contact, session])
        for path, table, columns in targets:
            image, log, receipt = reader.stable_image(path)
            overlay, pages, wal = reader.committed_wal(log)
            key = reader._keys[str(path)]
            mac = helper.mac_key(key, image[:4096])

            def decrypt(page, number):
                if len(page) != 4096 or not helper.authenticated(page, number, mac):
                    raise ValueError('微信目录页验证失败；不保存清单。')
                cipher = Cipher(algorithms.AES(key), modes.CBC(page[-80:-64])).decryptor()
                return (b'SQLite format 3\0' if number == 1 else b'') + cipher.update(page[16 if number == 1 else 0:-80]) + cipher.finalize() + b'\0' * 80

            view = reader.MemoryFile(image, overlay, pages or len(image) // 4096, decrypt)
            vfs = reader.MemoryVFS(view)
            connection = None
            try:
                connection = reader.apsw.Connection('file:authorized-directory?immutable=1',
                                                  flags=reader.apsw.SQLITE_OPEN_READONLY | reader.apsw.SQLITE_OPEN_URI,
                                                  vfs=vfs.name)

                def authorize(action, arg1, arg2, database, trigger):
                    if action == reader.apsw.SQLITE_READ:
                        return reader.apsw.SQLITE_OK if arg1 == table and arg2 in columns else reader.apsw.SQLITE_DENY
                    return reader.apsw.SQLITE_OK if action == reader.apsw.SQLITE_SELECT else reader.apsw.SQLITE_DENY

                connection.set_authorizer(authorize)
                rows = connection.execute('SELECT ' + ','.join(columns) + ' FROM ' + table + ' LIMIT 50001').fetchall()
                if len(rows) > 50000:
                    raise ValueError('目录超过本版核对上限，未保存不完整清单。')
                snapshots[table] = [dict(zip(columns, row)) for row in rows]
                receipt.update(wal=wal, columns=list(columns), rows=len(rows), message_body_read=False)
                receipts.append(receipt)
            finally:
                if connection:
                    connection.close()
                vfs.unregister()
            del image, log, overlay, view
    active = {row['username']: row.get('last_timestamp') for row in snapshots['SessionTable']}
    entries = []
    for row in snapshots['contact']:
        identity = row.get('username')
        if not identity:
            continue
        entries.append({'identity': identity, 'name': row.get('remark') or row.get('nick_name') or identity,
                        'remark': row.get('remark') or '', 'nickname': row.get('nick_name') or '',
                        'alias': row.get('alias') or '', 'group': identity.endswith('@chatroom'),
                        'deleted': bool(row.get('delete_flag')), 'in_group': row.get('is_in_chat_room'),
                        'has_local_session': identity in active, 'last_local_message_at': active.get(identity),
                        'work_scope': 'unreviewed'})
    known = {entry['identity'] for entry in entries}
    for identity, timestamp in active.items():
        if identity not in known:
            entries.append({'identity': identity, 'name': identity, 'group': identity.endswith('@chatroom'),
                            'has_local_session': True, 'last_local_message_at': timestamp, 'work_scope': 'unreviewed'})
    folder = database_path().parent / 'setup'
    folder.mkdir(exist_ok=True)
    output = folder / ('wechat-directory-' + uuid.uuid4().hex[:12] + '.json')
    report = {'confirmed_at': dt.datetime.now().astimezone().isoformat(timespec='seconds'),
              'account_id': config['account_id'], 'message_bodies_read': 0,
              'keys_saved': False, 'entries': entries, 'receipts': receipts}
    with output.open('x', encoding='utf-8') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    return {'ok': True, 'entries': len(entries), 'output': str(output),
            'message_bodies_read': 0, 'approved_sources_added': 0,
            'next': '只用元数据帮助本人选择工作来源；此清单不授权读取聊天。'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--confirm-directory', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(inventory(args.confirm_directory), ensure_ascii=False, indent=2))
    except Exception as error:
        print(json.dumps({'ok': False, 'error': str(error)}, ensure_ascii=False))
        sys.exit(1)
