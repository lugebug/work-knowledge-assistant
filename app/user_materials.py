"""Archive explicitly supplied work text without claiming a remote collection."""
import datetime as dt
import re
from urllib.parse import urlparse

from raw_store import source_key
from store import StoreError, json_text, now


def import_document(raw, document):
    allowed = {'key', 'title', 'body', 'url', 'source_date', 'basis', 'expected_version'}
    if not isinstance(document, dict) or set(document) - allowed:
        raise StoreError('请提供明确交给知识库的文字、名称、稳定标识和来源说明。')
    key = document.get('key', '')
    title, body, basis = (document.get(k, '') for k in ('title', 'body', 'basis'))
    if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{2,159}', key):
        raise StoreError('用户资料需要3至160字符的稳定标识。')
    if any(not isinstance(v, str) or not v.strip() for v in (title, body, basis)):
        raise StoreError('资料名称、原文及用户提供依据不可为空。')
    if len(title) > 3000 or len(body) > 1000000 or len(basis) > 4000:
        raise StoreError('用户资料超过本次文字保存上限。')
    url, source_date = document.get('url', ''), document.get('source_date', '')
    if not isinstance(url, str) or len(url) > 4000:
        raise StoreError('来源链接格式不正确。')
    parsed = urlparse(url)
    if url and (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password):
        raise StoreError('来源链接应为不含登录凭据的HTTPS链接；本入口不会访问链接。')
    try:
        valid_date = isinstance(source_date, str) and (not source_date or dt.date.fromisoformat(source_date).isoformat() == source_date)
    except ValueError:
        valid_date = False
    if not valid_date:
        raise StoreError('资料发生日期应为年-月-日；未知时留空，不能用提交日期替代。')
    sid = source_key('user', 'document', key)
    record = dict(source_id=sid, object_id='user:' + key, kind='document', title=title,
                  body=body, url=url, event_at=source_date, revision='', sender='用户提供',
                  content_mode='user_supplied_text', origin_note=basis)
    approved_head = None
    with raw.store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        old = c.execute('SELECT * FROM raw_sources WHERE id=?', (sid,)).fetchone()
        if old:
            source = raw.source(sid, True, c)
            if not source['config'].get('manual_only'):
                raise StoreError('该身份不是用户提供资料，不能改变已有来源。', 409)
            head = c.execute('SELECT v.id,v.payload FROM raw_heads h JOIN raw_versions v ON v.id=h.version_id WHERE h.source_id=? AND h.object_id=?', (sid, record['object_id'])).fetchone()
            if head and json_text(record) == head['payload']:
                return dict(source_id=sid, version_id=head['id'], unchanged=True, new=0, changed=0)
            if head and document.get('expected_version') != head['id']:
                raise StoreError('资料已有版本，请先核对当前原文并提供expected_version，不能用旧文本替换。', 409)
            approved_head = head['id'] if head else None
        else:
            config = dict(manual_only=True, text_only=True, since=now(), url=url)
            c.execute('INSERT INTO raw_sources VALUES(?,?,?,?,?,?,?,?,?,?)',
                      (sid, 'user', 'document', key, title, 'approved', json_text(config),
                       json_text({'note': basis, 'origin': '用户在当前对话明确提供'}), 1, now()))
            source = raw.source(sid, True, c)
            c.execute('INSERT INTO raw_source_history(source_id,before_json,after_json,actor,happened_at) VALUES(?,?,?,?,?)',
                      (sid, '{}', json_text(source), '用户提供文字，Codex归档', now()))
    run = raw.begin(sid)
    try:
        with raw.store.connect() as c:
            current = c.execute('SELECT version_id FROM raw_heads WHERE source_id=? AND object_id=?', (sid, record['object_id'])).fetchone()
            if (current[0] if current else None) != approved_head:
                raise StoreError('资料在本次保存前已更新，请读取新版本后重新核对。', 409)
        result = raw.commit(run, [record], {'complete': True, 'coverage': '仅用户本次提供的完整文字',
                           'manual_only': True, 'remote_verified': False})
    except Exception:
        raw.fail(run['id'], '本次用户文字归档未完成；未登记业务整理结果。')
        raise
    with raw.store.connect() as c:
        version_id = c.execute('SELECT version_id FROM raw_heads WHERE source_id=? AND object_id=?', (sid, record['object_id'])).fetchone()[0]
    return dict(result, source_id=sid, version_id=version_id)
