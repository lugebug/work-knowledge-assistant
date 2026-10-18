"""Bounded, deny-by-default source access. Never enumerate personal contacts."""
import datetime as dt
import json
import re
from pathlib import Path

SCOPE_FILE = Path(__file__).with_name('work_scope.json')

def scope():
    return json.loads(SCOPE_FILE.read_text('utf-8'))

def require_conversation(platform, conversation_id, conn=None):
    if conn is not None and conn.execute("SELECT 1 FROM sqlite_master WHERE name='raw_sources'").fetchone():
        row=conn.execute("SELECT state,name,config FROM raw_sources WHERE platform=? AND kind='chat' AND identity=?",(platform,conversation_id)).fetchone()
        if row:
            if row['state']!='approved':raise ValueError('该会话不在已批准工作范围；未读取正文。')
            return dict(platform=platform,id=conversation_id,name=row['name'],**json.loads(row['config']))
    match = next((x for x in scope()['conversations'] if x['platform'] == platform and x['id'] == conversation_id), None)
    if not match:
        raise ValueError('该会话未列入工作读取范围；未读取正文。')
    return match

def read_document(conn, docid):
    if docid not in scope()['documents']:
        raise ValueError('该文档未列入工作读取范围；未读取正文。')
    row = conn.execute('SELECT id,title,body,revision,kind,url FROM documents WHERE id=?', (docid,)).fetchone()
    if not row:
        raise ValueError('工作来源文档不存在。')
    return dict(row)

def search_messages(conn, platform, conversation_id, since, until, keyword, limit=30):
    require_conversation(platform, conversation_id, conn)
    start, end = dt.date.fromisoformat(since), dt.date.fromisoformat(until)
    if end < start or (end-start).days > 92 or not keyword.strip() or not 1 <= limit <= 100:
        raise ValueError('请给出最多93天的日期范围、工作关键词及1至100条结果上限。')
    has_raw=bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='raw_heads'").fetchone())
    # A newer version can remove a keyword. Exclude superseded legacy objects
    # before filtering/limiting so their obsolete text cannot reappear.
    current_only=''' AND NOT EXISTS(SELECT 1 FROM raw_heads h JOIN raw_sources s ON s.id=h.source_id
        WHERE s.platform=messages.platform AND s.identity=messages.conversation_id
        AND s.kind='chat' AND s.state='approved' AND h.object_id=messages.id)''' if has_raw else ''
    rows = [dict(r) for r in conn.execute('''SELECT id,platform,conversation_id,conversation_name,sender_name,event_at,body,url
        FROM messages WHERE platform=? AND conversation_id=? AND event_at>=? AND event_at<? AND instr(body,?)>0
        '''+current_only+' ORDER BY event_at DESC LIMIT ?', (platform, conversation_id, since, (end+dt.timedelta(days=1)).isoformat(), keyword, limit))]
    if has_raw:
        newer=[]
        for r in conn.execute('''SELECT v.id,v.object_id,v.payload FROM raw_heads h JOIN raw_versions v ON v.id=h.version_id
          JOIN raw_sources s ON s.id=h.source_id WHERE s.platform=? AND s.kind='chat' AND s.identity=? AND s.state='approved'
          AND json_extract(v.payload,'$.event_at')>=? AND json_extract(v.payload,'$.event_at')<?
          AND instr(json_extract(v.payload,'$.body'),?)>0 ORDER BY json_extract(v.payload,'$.event_at') DESC LIMIT ?''',
          (platform,conversation_id,since,(end+dt.timedelta(days=1)).isoformat(),keyword,limit)):
            p=json.loads(r['payload']);newer.append(dict(id=r['id'],object_id=r['object_id'],reference_type='raw',platform=platform,conversation_id=conversation_id,
                conversation_name=p['title'],sender_name=p['sender'],event_at=p['event_at'],body=p['body'],url=p['url']))
        replaced={r['object_id'] for r in newer}
        rows=sorted(newer+[r for r in rows if r['id'] not in replaced],key=lambda r:r['event_at'],reverse=True)[:limit]
    for row in rows:
        if re.search(r'(密码|口令|password|api[_ -]?key)\s*[:：=]', row['body'], re.I):
            row['body'] = '[含登录凭据，正文未输出；此项不用于业务事实整理]'
    return rows

def resolve_reference(conn, ref):
    """Validate scope before fetching source content; quoted excerpts must match."""
    if not isinstance(ref, dict) or set(ref) != {'type','id','quote'} or not isinstance(ref['id'],str) or not isinstance(ref['type'],str) or not isinstance(ref['quote'], str) or not 2 <= len(ref['quote']) <= 2000:
        raise ValueError('依据需要来源类型、标识和2至2000字的原文摘录。')
    if ref['type'] == 'message':
        row = conn.execute('SELECT platform,conversation_id FROM messages WHERE id=?', (ref['id'],)).fetchone()
        if not row: raise ValueError('消息依据不存在。')
        allowed = require_conversation(row['platform'], row['conversation_id'], conn)
        row = conn.execute('SELECT body,event_at,url,sender_name FROM messages WHERE id=? AND platform=? AND conversation_id=?', (ref['id'], allowed['platform'], allowed['id'])).fetchone()
        result = dict(ref, title=allowed['name'], event_at=row['event_at'], url=row['url'], sender=row['sender_name'])
    elif ref['type'] == 'document':
        row = read_document(conn, ref['id'])
        result = dict(ref, title=row['title'], revision=row['revision'], url=row['url'], source_kind=row['kind'])
    elif ref['type']=='profile':
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='wk_person_sources'").fetchone():raise ValueError('人员依据快照不存在。')
        row=conn.execute('SELECT title,body,origin,observed_at FROM wk_person_sources WHERE id=?',(ref['id'],)).fetchone()
        if not row:raise ValueError('人员依据快照不存在。')
        result=dict(ref,title=row['title'],event_at=row['observed_at'],source_kind='已核对工作资料快照',origin=row['origin'])
    elif ref['type']=='raw':
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='raw_versions'").fetchone():raise ValueError('原始版本不存在。')
        pointer=conn.execute('SELECT source_id FROM raw_versions WHERE id=?',(ref['id'],)).fetchone()
        if not pointer:raise ValueError('原始版本不存在。')
        source=conn.execute('SELECT name,state FROM raw_sources WHERE id=?',(pointer[0],)).fetchone()
        if not source or source['state']!='approved':raise ValueError('来源未批准；未读取原文。')
        p=json.loads(conn.execute('SELECT payload FROM raw_versions WHERE id=?',(ref['id'],)).fetchone()[0])
        row={'body':p['body']}
        result=dict(ref,title=p['title'],event_at=p.get('event_at',''),url=p.get('url',''),sender=p.get('sender',''),revision=p.get('revision',''),source_kind='不可覆盖的原始资料版本')
    else:
        raise ValueError('只支持已列入范围的工作消息和文档。')
    if re.search(r'(密码|口令|password|api[_ -]?key)\s*[:：=]', ref['quote'], re.I):
        raise ValueError('登录凭据不能作为业务依据保存。')
    if ref['quote'] not in row['body']:
        raise ValueError('摘录与来源不一致，不能保存为依据。')
    return result
