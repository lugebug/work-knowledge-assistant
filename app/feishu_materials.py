"""Discover visible Feishu text documents and minute-linked notes before collection.

The inventory is metadata only. Existing RAW sources retain their authorization;
new bodies are read solely by the existing versioned document collector.
"""
import datetime as dt
import html
import re
from urllib.parse import urlparse

from raw_collectors import lark_read
from store import StoreError, now
from feishu_shared_materials import shared_links, minute_entry

from runtime_config import history_since, require_feishu
SINCE = history_since()


def pages(read, args, key):
    items=[]; token=None; seen=set()
    for _ in range(100):
        data=read([*args,*(['--page-token',token] if token else [])])
        if not isinstance(data.get(key),list) or type(data.get('has_more')) is not bool:
            raise StoreError('飞书资料目录缺少分页完整性信息；未登记部分目录。')
        items.extend(data[key])
        if not data['has_more']:return items
        token=data.get('page_token') or data.get('next_page_token')
        if not token or token in seen:raise StoreError('飞书资料目录分页标识缺失或循环；未登记部分目录。')
        seen.add(token)
    raise StoreError('飞书资料目录超过分页上限；未登记部分目录。')


def clean_title(value):
    return html.unescape(re.sub(r'<[^>]*>','',str(value))).strip()


def discover(raw, read=lark_read, since=SINCE, register=True):
    require_feishu(discovery=True)
    start=dt.datetime.fromisoformat(since)
    if start.tzinfo is None or start<dt.datetime.fromisoformat(SINCE):
        raise StoreError('资料发现起点必须带时区，且不得早于本机已确认的历史起点。')
    end=now(); errors=[]; docs={}; skipped=[]
    queries=[
        ['drive','+search','--created-since',since,'--doc-types','docx,doc','--sort','edit_time','--page-size','20'],
        ['drive','+search','--doc-types','docx,doc','--sort','edit_time','--page-size','20'],
    ]
    # Feishu's unfiltered and date-filtered searches expose different result sets;
    # use their union, not an assumption that one first page is the whole drive.
    found=[]
    for args in queries:found.extend(pages(read,args,'results'))
    for item in found:
        meta=item.get('result_meta') or {}; identity=meta.get('token'); url=meta.get('url','')
        if not identity or not re.fullmatch(r'[A-Za-z0-9]{10,100}',identity):
            raise StoreError('飞书文档目录缺少准确文档身份；未登记。')
        typ=str(meta.get('doc_types','')).upper()
        if typ not in ('DOC','DOCX'):
            skipped.append({'identity':identity,'reason':'当前只归档文字文档','type':typ});continue
        times=[]
        for k in ('create_time','update_time'):
            value=meta.get(k)
            if value is not None:
                try:times.append(dt.datetime.fromtimestamp(int(value),start.tzinfo))
                except (ValueError,TypeError,OverflowError):raise StoreError('飞书文档目录时间无效；未登记。') from None
        if not times:raise StoreError('飞书文档目录缺少创建或更新时间；未登记。')
        if max(times)<start:continue
        parsed=urlparse(url)
        if parsed.scheme!='https' or not (parsed.hostname or '').endswith(('.feishu.cn','.larksuite.com','.larkoffice.com')):
            errors.append({'identity':identity,'name':clean_title(item.get('title_highlighted','')),
                           'url':url,'reason':'目录未提供可核验的飞书文档链接'});continue
        wiki_token=None
        if parsed.path=='/wiki/'+identity:
            wiki_token=identity
            try:
                inspected=read(['drive','+inspect','--url',url])
                if inspected.get('type') not in ('doc','docx') or (inspected.get('wiki_node') or {}).get('node_token')!=wiki_token:
                    raise StoreError('知识库节点未解析为同一条文字文档。')
                identity=inspected.get('token','');url=inspected.get('url','')
                if not re.fullmatch(r'[A-Za-z0-9]{10,100}',identity):raise StoreError('知识库文档身份缺失。')
            except (StoreError,OSError) as exc:
                errors.append({'identity':wiki_token,'reason':str(exc)});continue
        if urlparse(url).path not in ('/docx/'+identity,'/docs/'+identity):
            errors.append({'identity':identity,'reason':'目录链接不是可直接读取的文字文档'});continue
        docs[identity]=dict(identity=identity,name=clean_title(item.get('title_highlighted') or identity),
            url=url.split('#',1)[0],since=since,metadata={k:meta[k] for k in
            ('owner_id','owner_name','create_time','update_time','doc_types') if k in meta},
            basis={'note':'用户确认所有可见飞书资料为工作来源；9月1日起新建或更新的文字资料',
                   'evidence':['当前已登录飞书文档目录',{'identity':identity,'url':url}]})
        if wiki_token:docs[identity]['metadata']['wiki_node_token']=wiki_token
    minutes=pages(read,['minutes','+search','--start',since,'--end',end,'--page-size','30'],'items')
    unique={}
    for minute in minutes:
        token=minute.get('token','')
        if not re.fullmatch(r'[A-Za-z0-9]{10,100}',token):raise StoreError('妙记目录缺少稳定身份；未登记。')
        unique[token]=minute
    linked=0; fallback=[]
    for token,minute in unique.items():
        minute_url=(minute.get('meta_data') or {}).get('app_link','')
        try:
            data=read(['minutes','+detail','--minute-tokens',token])
            matches=[m for m in data.get('minutes',[]) if m.get('minute_token')==token]
            if len(matches)!=1:
                raise StoreError('妙记目录与返回身份不符；不能登记其他资源。',409)
            if not matches[0].get('note_id'):
                raise StoreError('妙记尚无可读取的纪要／文字记录链接；下轮继续核对。')
            detail=matches[0];note_id=str(detail['note_id'])
            note=read(['note','+detail','--note-id',note_id]).get('note',{})
            if str(note.get('note_id',''))!=note_id:raise StoreError('会议记录身份与妙记不符。',409)
            minute_url=(minute.get('meta_data') or {}).get('app_link','')
            parsed=urlparse(minute_url)
            if parsed.scheme!='https' or not (parsed.hostname or '').endswith(('.feishu.cn','.larksuite.com','.larkoffice.com')) or parsed.path!='/minutes/'+token:
                raise StoreError('妙记链接无效；未按未知链接登记。')
            count=0
            for field,role,prefix in [('note_doc_token','ai_summary','智能纪要：'),('verbatim_doc_token','transcript','文字记录：')]:
                identity=note.get(field)
                if not identity:continue
                if not re.fullmatch(r'[A-Za-z0-9]{10,100}',identity):raise StoreError('会议文字文档身份无效。')
                extra={'minute_token':token,'note_id':note_id,'minute_url':minute_url,'document_role':role,
                       'meeting_description':(minute.get('meta_data') or {}).get('description','')}
                if identity in docs:docs[identity]['metadata'].update(extra)
                else:
                    docs[identity]=dict(identity=identity,name=prefix+detail.get('title',token),
                        url=f'{parsed.scheme}://{parsed.netloc}/docx/{identity}',since=since,metadata=extra,
                        basis={'note':'可见飞书工作妙记的文字记录；AI纪要须对照转写核验',
                               'evidence':[{'minute_token':token,'note_id':note_id,'url':minute_url}]})
                count+=1
            if not count:raise StoreError('妙记未生成文字文档；下轮继续核对。')
            linked+=1
        except (StoreError,OSError) as exc:
            if isinstance(exc,StoreError) and exc.status==409:
                errors.append({'minute_token':token,'reason':str(exc)});continue
            # A minute can expose its own text even if it has no Docx, or its
            # linked note has separate access restrictions. Collection checks
            # the minute's own permission and requires a complete transcript.
            try:
                title=clean_title((minute.get('display_info') or '').split('\n',1)[0])
                docs[token]=minute_entry(token,title,minute_url,since,
                    {'meeting_description':(minute.get('meta_data') or {}).get('description',''),
                     'linked_note_status':str(exc)},
                    [{'minute_token':token,'url':minute_url}])
                fallback.append({'minute_token':token,'reason':str(exc)})
            except StoreError as invalid:
                errors.append({'minute_token':token,'reason':str(invalid)})
    shared=shared_links(raw,read,since,set(docs)|set(unique))
    for entry in shared['entries']:docs.setdefault(entry['identity'],entry)
    errors.extend(shared['errors']);skipped.extend(shared['skipped'])
    registered=[]; protected=[]
    if register:
        for identity,entry in sorted(docs.items()):
            result=raw.register_work_document(**entry)
            if result.get('protected'):protected.append(result['id'])
            elif not result.get('unchanged'):registered.append(result['id'])
    return dict(since=since,until=end,search_results=len(found),documents=len(docs),visible_minutes=len(unique),
                linked_minutes=linked,fallback_minutes=fallback,shared_links_checked=shared['links_checked'],
                shared_documents=len(shared['entries']),added_sources=len(registered),registered=registered,protected=protected,
                skipped=skipped,errors=errors,complete=not errors,entries=list(docs.values()))
