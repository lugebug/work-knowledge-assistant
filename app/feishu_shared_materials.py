"""Find text resources shared in already-approved Feishu source versions."""
import datetime as dt
import html
import json
import re
from urllib.parse import urlparse

from store import StoreError

LINK = re.compile(r'https://[A-Za-z0-9.-]+\.(?:feishu\.cn|larksuite\.com|larkoffice\.com)/(?:docx|docs|wiki|minutes)/[A-Za-z0-9]{10,100}')


def shared_links(raw, read, since, known):
    """Read only approved Feishu heads. Never examine WeChat or paused bodies."""
    links = {}; entries = {}; errors = []; skipped = []
    start = dt.datetime.fromisoformat(since)
    with raw.store.connect() as c:
        protected = {r['identity'] for r in c.execute("SELECT identity FROM raw_sources WHERE platform='feishu' AND state!='approved'")}
        rows = c.execute("""SELECT v.id,v.payload,s.kind FROM raw_versions v
            JOIN raw_heads h ON h.version_id=v.id JOIN raw_sources s ON s.id=v.source_id
            WHERE s.platform='feishu' AND s.state='approved'""").fetchall()
    for row in rows:
        payload = json.loads(row['payload'])
        if row['kind'] == 'chat':
            if not payload.get('event_at') or dt.datetime.fromisoformat(payload['event_at']) < start:
                continue
        for url in LINK.findall(html.unescape(payload.get('body', ''))):
            links.setdefault(url, []).append(row['id'])
    for url, versions in sorted(links.items()):
        token = url.rsplit('/', 1)[-1]
        if token in known or token in protected:
            continue
        if '/minutes/' in url:
            # The minute directory resolves these separately; preserve links
            # outside that directory as independently collectable text sources.
            try:
                meta=read(['minutes','minutes','get','--minute-token',token]).get('minute',{})
                if meta.get('token')!=token:raise StoreError('分享妙记元数据身份不一致。')
                created=dt.datetime.fromtimestamp(int(meta['create_time'])/1000,start.tzinfo)
                if created<start:
                    skipped.append({'url':url,'reason':'妙记创建时间早于本阶段9月1日'});continue
                entries[token] = minute_entry(token, meta.get('title') or '聊天分享的飞书妙记', url, since,
                    {'shared_in': versions,'create_time':meta['create_time']}, [{'type': 'raw', 'id': v} for v in versions])
            except (StoreError,OSError,KeyError,ValueError,TypeError,OverflowError) as exc:
                errors.append({'url':url,'reason':str(exc) if isinstance(exc,(StoreError,OSError)) else '分享妙记时间元数据不完整'})
            continue
        try:
            inspected = read(['drive', '+inspect', '--url', url])
            identity = inspected.get('token', '')
            if inspected.get('type') not in ('doc', 'docx'):
                skipped.append({'url': url, 'type': inspected.get('type'), 'reason': '本阶段只归档文字文档'})
                continue
            if not re.fullmatch(r'[A-Za-z0-9]{10,100}', identity):
                raise StoreError('分享链接没有返回准确文档身份。')
            if '/wiki/' in url and (inspected.get('wiki_node') or {}).get('node_token') != token:
                raise StoreError('分享的知识库节点身份不一致。')
            if '/wiki/' not in url and identity != token:
                raise StoreError('分享文档身份不一致。')
            if identity in known or identity in protected:
                continue
            data=read(['drive','metas','batch_query','--data',json.dumps({'request_docs':[{'doc_token':identity,'doc_type':inspected['type']}],'with_url':True})])
            matches=[m for m in data.get('metas',[]) if m.get('doc_token')==identity]
            if len(matches)!=1:raise StoreError('分享文档的时间元数据不可读取；未越过阶段范围。')
            meta=matches[0]
            try:
                changed=max(dt.datetime.fromtimestamp(int(meta[k]),start.tzinfo) for k in ('create_time','latest_modify_time'))
            except (KeyError,ValueError,TypeError,OverflowError):raise StoreError('分享文档时间元数据无效。') from None
            if changed<start:
                skipped.append({'url':url,'reason':'文档创建及修改均早于本阶段9月1日'});continue
            entries[identity] = dict(identity=identity,
                name=meta.get('title') or inspected.get('title') or '聊天分享的飞书文字文档',
                url=inspected.get('url', ''), since=since,
                metadata={'shared_in': versions, 'shared_url': url,'create_time':meta['create_time'],'update_time':meta['latest_modify_time']},
                basis={'note': '用户授权补查飞书工作资料；来源为已批准的阶段内文字资料中的分享链接',
                       'evidence': [{'type': 'raw', 'id': v, 'url': url} for v in versions]})
        except (StoreError, OSError) as exc:
            errors.append({'url': url, 'reason': str(exc)})
    return {'entries': list(entries.values()), 'links_checked': len(links), 'errors': errors, 'skipped': skipped}


def minute_entry(token, title, url, since, metadata, evidence):
    parsed = urlparse(url)
    if not re.fullmatch(r'[A-Za-z0-9]{10,100}', token) or parsed.scheme != 'https' or not (parsed.hostname or '').endswith(('.feishu.cn', '.larksuite.com', '.larkoffice.com')) or parsed.path != '/minutes/' + token:
        raise StoreError('妙记文字来源身份或链接无效。')
    return dict(identity=token, name=title or '飞书妙记文字内容', url=url, since=since,
        metadata=dict(metadata, document_role='minute_artifacts', minute_token=token),
        basis={'note': '可见工作妙记直接提供的摘要、待办及文字转写；不读取音视频', 'evidence': evidence})
