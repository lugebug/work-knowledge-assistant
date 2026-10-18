"""Read-only source adapters. Each receives an already approved exact source."""
import datetime as dt
import html
import json
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET
from store import StoreError

from runtime_config import lark_command, require_feishu
TZ=dt.timezone(dt.timedelta(hours=8))

def visible_text(text, platform):
    if not isinstance(text,str):raise StoreError('来源正文格式不支持。')
    if platform=='wechat':
        text=re.sub(r'^[^\s<>:]{1,100}:\n','',text)
        if text.lstrip().startswith('<'):
            try:
                root=ET.fromstring(text[text.find('<'):])
                text=' '.join(x for x in (root.findtext('.//title'),root.findtext('.//des')) if x) or '[非文字消息；媒体未解析]'
            except ET.ParseError:text='[结构化消息；未解析]'
    else:
        # Retain hyperlink destinations so shared documents can be discovered
        # even when their display text is only a title.
        text=re.sub(r'<a\b[^>]*href="(https?://[^"<>]+)"[^>]*>(.*?)</a>',lambda m:m[2]+' '+m[1],text,flags=re.S|re.I)
        text=re.sub(r'<file\b[^>]*name="([^"]*)"[^>]*/?>',lambda m:'[文件：'+m[1]+']',text)
        text=re.sub(r'<image\b[^>]*>','[图片；未解析]',text)
        text=re.sub(r'<[^>]+>','',text)
    text=html.unescape(text).strip()
    # Archive visible business text, never opaque attachment credentials or auth tokens.
    text=re.sub(r'(https?://[^\s<>]*[?&](?:token|key|auth|signature|secret|aeskey)=)[^\s<>]+',r'\1[已隐藏]',text,flags=re.I)
    text=re.sub(r'(?im)((?:密码|口令|password|api[_ -]?key|access[_ -]?token|secret)\s*[:：=]\s*)\S+',r'\1[已隐藏]',text)
    return text

def event_time(value):
    value=str(value)
    if value.isdigit():
        number=int(value);number=number/1000 if number>10**11 else number
        return dt.datetime.fromtimestamp(number,TZ).isoformat(timespec='seconds')
    date=dt.datetime.fromisoformat(value.replace('Z','+00:00'))
    if date.tzinfo is None:date=date.replace(tzinfo=TZ)
    return date.astimezone(TZ).isoformat(timespec='seconds')

def lark_read(args, cwd=None):
    require_feishu()
    if tuple(args[:2]) not in {('im','+chat-messages-list'),('im','+messages-search'),('im','+chat-list'),('docs','+fetch'),
            ('drive','+search'),('drive','+inspect'),('minutes','+search'),('minutes','+detail'),('note','+detail'),
            ('task','+get-related-tasks'),('task','+get-my-tasks')} and tuple(args[:3]) not in {
                ('drive','metas','batch_query'),('minutes','minutes','get'),('task','tasklists','list'),
                ('task','tasklists','get'),('task','tasklists','tasks'),('task','tasks','get'),('task','subtasks','list')}:
        raise StoreError('不允许的采集命令。')
    try:
        p=subprocess.run([*lark_command(),*args,'--as','user'],capture_output=True,encoding='utf-8',errors='replace',timeout=90,cwd=cwd,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    except subprocess.TimeoutExpired:
        raise StoreError('飞书读取超时；未推进完成位置，可重试。',503) from None
    payload=None
    for stream in (p.stdout,p.stderr):
        for m in re.finditer(r'(?m)^\s*\{',stream):
            try:
                obj,_=json.JSONDecoder().raw_decode(stream[m.start():].lstrip())
                if isinstance(obj,dict):payload=obj;break
            except ValueError:pass
        if payload is not None:break
    if p.returncode or not payload or not payload.get('ok'):
        code=(payload or {}).get('error',{}).get('code','unknown')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',str(code)):code='unknown'
        raise StoreError(f'飞书读取失败（{code}）；检查登录或来源权限后重试，未推进完成位置。',503)
    return payload['data']

class FeishuCollector:
    def __init__(self, read=lark_read, artifact_dir=None):self.read=read;self.artifact_dir=artifact_dir
    def collect(self, run):
        s=run['source']
        if s['platform']!='feishu' or s['state']!='approved':raise StoreError('未批准的飞书来源。',403)
        if s['kind']=='task':
            from feishu_tasks import collect_task
            return collect_task(run,self.read)
        if s['kind']=='document':
            if s['config'].get('discovery_metadata',{}).get('document_role')=='minute_artifacts':
                from feishu_minute_text import collect_minute
                return collect_minute(run,lambda args,cwd:self.read(args,cwd=cwd),self.artifact_dir)
            data=self.read(['docs','+fetch','--doc',s['identity'],'--doc-format','xml'])
            d=data.get('document',{})
            if d.get('document_id')!=s['identity'] or not isinstance(d.get('content'),str) or d.get('revision_id') is None:
                raise StoreError('文档身份或版本缺失；不保存为完整来源。')
            raw=d['content']
            # Preserve document XML for fidelity; redact credential-bearing strings only.
            body=re.sub(r'(?im)((?:密码|口令|password|api[_ -]?key|access[_ -]?token|secret)\s*[:：=]\s*)[^\s<]+',r'\1[已隐藏]',raw)
            body=re.sub(r'((?:密码|口令)\s*(?:为|是)\s*)[A-Za-z0-9_!@#$%&*+./=-]{4,}',r'\1[已隐藏]',body)
            title=re.search(r'<title>(.*?)</title>',body,re.S)
            record={'source_id':s['id'],'object_id':'feishu:'+s['identity'],'kind':'document','body':body,'title':html.unescape(title[1]) if title else s['name'],
                    'revision':str(d['revision_id']),'url':s['config'].get('url',''),'event_at':'','sender':'','content_mode':'document_xml'}
            return [record],{'complete':True,'pages':1,'revision':record['revision'],'coverage':'已登记文档的本次修订'}
        if s['kind']!='chat':raise StoreError('不支持的飞书来源类型。')
        queries=s['config'].get('topics') or [None]
        records={};pages=0;skipped={}
        for query in queries:
            token=None;seen=set()
            for _ in range(100):
                page_size=s['config'].get('page_size',50)
                if type(page_size) is not int or not 1<=page_size<=50:raise StoreError('分页大小无效。')
                args=['im','+messages-search' if query else '+chat-messages-list','--chat-id',s['identity'],'--start',run['since'],'--end',run['until'],'--page-size',str(page_size),'--no-reactions']
                if query:args+=['--query',query]
                else:args+=['--order','asc']
                if token:args+=['--page-token',token]
                data=self.read(args);pages+=1
                if not isinstance(data.get('messages'),list) or type(data.get('has_more')) is not bool:
                    raise StoreError('飞书分页完整性信息缺失；未提交批次。')
                for m in data['messages']:
                    if m.get('chat_id')!=s['identity']:raise StoreError('接口返回了范围外会话，拒绝保存。',403)
                    if not m.get('message_id'):raise StoreError('消息缺少稳定标识。')
                    typ=m.get('msg_type','')
                    if s['config'].get('text_only') and typ not in ('text','post'):
                        skipped[typ]=skipped.get(typ,0)+1;continue
                    content=m.get('content','')
                    if s['config'].get('text_only'):
                        content=re.sub(r'<(?:image|file|audio|video)\b[^>]*(?:/>|>.*?</(?:image|file|audio|video)>)','',content,flags=re.S)
                    body='[消息已撤回或删除；正文不可用]' if m.get('deleted') is True else visible_text(content,'feishu')
                    if s['config'].get('text_only') and not body:continue
                    sender=m.get('sender') or {}
                    r={'source_id':s['id'],'object_id':'feishu:'+m['message_id'],'kind':'message',
                       'body':body,'title':s['name'],'revision':'',
                       'url':m.get('message_app_link',''),'event_at':event_time(m['create_time']),
                       'sender':sender.get('name') or sender.get('id',''),'sender_id':sender.get('id',''),
                       'message_type':m.get('msg_type',''),'content_mode':'visible_text','sender_type':sender.get('sender_type','')}
                    if m.get('deleted') is True:
                        r['source_deleted']=True
                    if r['object_id'] in records and records[r['object_id']]!=r:raise StoreError('分页中消息发生变化，请重试。')
                    records[r['object_id']]=r
                if not data['has_more']:break
                token=data.get('page_token') or data.get('next_page_token')
                if not token or token in seen:raise StoreError('分页标识缺失或循环；未提交批次。')
                seen.add(token)
            else:raise StoreError('超过单次分页上限；保留失败状态，请缩小初始读取范围。')
        return list(records.values()),{'complete':True,'pages':pages,'queries':queries,'skipped_types':skipped,'coverage':'项目主题搜索可见结果' if queries!=[None] else '指定会话时间窗','has_more':False}

def update_one(raw, source_id, retry_of=None, collector=None):
    if raw.source(source_id,True)['config'].get('manual_only'):
        raise StoreError('这是用户提供的文字资料；请提交新文字版本，不进行远程采集。')
    run=raw.begin(source_id,retry_of)
    return collect_run(raw,run,collector)

def collect_run(raw,run,collector=None):
    try:
        if run['source']['config'].get('manual_only'):
            raise StoreError('用户提供文字不能通过远程采集更新。')
        if collector is None:
            if run['source']['platform']=='feishu':collector=FeishuCollector(artifact_dir=raw.root/'minute-artifacts')
            elif run['source']['platform']=='wechat':
                from raw_wechat import WeChatCollector
                collector=WeChatCollector(raw)
            else:raise StoreError('该来源尚无采集适配。')
        records,receipt=collector.collect(run)
        if receipt.get('complete') is not True:raise StoreError('来源未完整读取，本轮未提交。')
        result=raw.commit(run,records,receipt)
        # Discovery cannot roll back a successfully persisted source batch.
        try:result['discovered_candidates']=len(raw.discover_wechat_candidates(run['id']))
        except Exception:result['candidate_review']='候选识别未完成；原始资料已保存，可单独复核。'
        return result
    except Exception as exc:
        reason=str(exc) if isinstance(exc,StoreError) else f'采集失败（{type(exc).__name__}）；未推进完成位置，可重试。'
        raw.fail(run['id'],reason)
        result={'run_id':run['id'],'source_id':run['source']['id'],'state':'failed','error':reason}
        if getattr(exc,'failure_scope','')=='wechat_shared_read':
            result['failure_scope']='wechat_shared_read'
        return result
