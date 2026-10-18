"""Read-only WCDB view: decrypt queried pages in memory, never export all plaintext.

SQLite VFS API: https://rogerbinns.github.io/apsw/vfs.html
Cipher scanner is the previously reviewed local helper, pinned by SHA-256.
Only exact approved conversation tables and their sender-id lookups are queried.
"""
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import struct
import subprocess
import sys
import threading
import uuid
from store import StoreError
from raw_collectors import visible_text, event_time

from runtime_config import settings, require_wechat
import apsw
import zstandard
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

HELPER = Path(__file__).with_name('wechat_crypto.py')
HELPER_HASH = '1ad5f056771e5cbbdfc6547158df6bf557d1bee5153d18e5d72f7f1144e8d940'
_guard=threading.Lock()
_keys={}
_identity=None

class WeChatReadError(StoreError):
    failure_scope = 'wechat_shared_read'


def crypto_helper():
    require_wechat()
    if hashlib.sha256(HELPER.read_bytes()).hexdigest()!=HELPER_HASH:
        raise WeChatReadError('本机微信只读适配文件校验变化，需重新核对；未读取消息。')
    spec=importlib.util.spec_from_file_location('reviewed_wechat_crypto',HELPER)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    module.emit=lambda *args,**kwargs:None
    return module

def current_processes():
    script="[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false); Get-CimInstance Win32_Process -Filter \"Name='Weixin.exe'\" | Select-Object ProcessId,ExecutablePath,CreationDate | ConvertTo-Json -Compress"
    p=subprocess.run(['powershell','-NoProfile','-Command',script],capture_output=True,encoding='utf-8',errors='replace',timeout=20,creationflags=subprocess.CREATE_NO_WINDOW)
    if p.returncode:raise WeChatReadError('无法核对本次微信进程；未读取消息。')
    rows=json.loads(p.stdout or '[]');rows=rows if isinstance(rows,list) else [rows]
    rows=[r for r in rows if str(r.get('ExecutablePath','')).casefold()==str(Path(settings()['wechat']['executable']).resolve()).casefold()]
    if not rows:raise WeChatReadError('本机微信未运行；上次成功数据保留，启动后可重试。',503)
    return rows

def ensure_keys(helper, paths):
    global _identity,_keys
    processes=current_processes()
    identity=tuple(sorted((r['ProcessId'],r['CreationDate']) for r in processes))
    if identity!=_identity:_keys={};_identity=identity
    first={}
    for path in paths:
        with path.open('rb') as f:first[str(path)]=f.read(4096)
        if str(path) in _keys and not helper.verify_candidate(_keys[str(path)],first[str(path)]):_keys.pop(str(path))
    if all(str(p) in _keys for p in paths):
        return {'verified_databases':len(paths),'cached_in_process':True}
    diagnostics={'processes':len(processes),'unreadable':0,'markers':0,'objects':0,'candidates':0}
    def observe(event, **values):
        # Keep aggregate stages only, never memory, pointers or candidate values.
        if event=='process_unreadable':diagnostics['unreadable']+=1
        elif event=='cipher_markers':diagnostics['markers']+=int(values.get('count',0))
        elif event=='scan_complete':
            diagnostics['objects']+=int(values.get('objects',0))
            diagnostics['candidates']+=int(values.get('candidate_count',0))
    previous_emit=helper.emit
    helper.emit=observe
    try:
        for process in processes:
            try:
                candidates=helper.scan(process['ProcessId'])
            except TimeoutError:
                raise WeChatReadError('微信读取条件检查超出本次只读扫描上限；未读取消息，完成位置保持。',503) from None
            for candidate in candidates:
                for name,page in first.items():
                    if name not in _keys and helper.verify_candidate(candidate,page):_keys[name]=candidate
            candidates.clear()
            if all(str(p) in _keys for p in paths):
                return dict(diagnostics,verified_databases=len(paths),cached_in_process=False)
    finally:
        helper.emit=previous_emit
    stage=('当前微信进程无法读取' if diagnostics['unreadable']==len(processes) else
           '未找到当前消息库读取对象' if not diagnostics['markers'] else
           '找到读取对象但未取得可校验信息' if not diagnostics['candidates'] else
           '读取信息未通过全部消息分库校验')
    raise WeChatReadError(f'微信读取条件未通过：{stage}；已验证{len(_keys)}/{len(paths)}个消息库。未读取会话正文，完成位置保持。',503)

def stable_image(path):
    """Ciphertext only. Two complete equal reads bind DB and WAL to one stable image."""
    wal=Path(str(path)+'-wal')
    for _ in range(3):
        db=path.read_bytes();log=wal.read_bytes() if wal.exists() else b''
        with path.open('rb') as f:db_hash=hashlib.file_digest(f,'sha256').hexdigest()
        if wal.exists():
            with wal.open('rb') as f:wal_hash=hashlib.file_digest(f,'sha256').hexdigest()
        else:wal_hash=hashlib.sha256(b'').hexdigest()
        if hashlib.sha256(db).hexdigest()==db_hash and hashlib.sha256(log).hexdigest()==wal_hash:
            return db,log,{'database':path.name,'encrypted_sha256':db_hash,'wal_sha256':wal_hash,'stable':True}
    raise StoreError('微信正在写入，连续读取未取得一致状态；本轮不提交，可重试。',503)

def checksum(data,endian,state=(0,0)):
    s0,s1=state
    for a,b in struct.iter_unpack(endian+'II',data):s0=(s0+a+s1)&0xffffffff;s1=(s1+b+s0)&0xffffffff
    return s0,s1

def committed_wal(data):
    if not data:return {},None,{'present':False,'committed_frames':0}
    if len(data)<32:raise StoreError('微信提交日志头不完整。')
    magic,version,page=struct.unpack_from('>III',data)
    if magic not in (0x377f0682,0x377f0683) or version!=3007000 or page!=4096:raise StoreError('微信提交日志格式未验证。')
    endian='<' if magic==0x377f0682 else '>';state=checksum(data[:24],endian)
    if state!=struct.unpack_from('>II',data,24):raise StoreError('微信提交日志头校验失败。')
    salt=data[16:24];pending={};committed={};count=valid=0;size=None;reason='end'
    for offset in range(32,len(data),4120):
        frame=data[offset:offset+4120]
        if len(frame)!=4120:reason='incomplete_tail';break
        if frame[8:16]!=salt:reason='stale_salt_tail';break
        pgno,commit_size=struct.unpack_from('>II',frame)
        if pgno==0:raise StoreError('微信日志页号无效。')
        next_state=checksum(frame[:8]+frame[24:],endian,state)
        if next_state!=struct.unpack_from('>II',frame,16):reason='invalid_checksum_tail';break
        state=next_state;valid+=1;pending[pgno]=frame[24:]
        if commit_size:
            committed.update(pending);pending={};size=commit_size;count=valid
    return committed,size,{'present':True,'valid_frames':valid,'committed_frames':count,'uncommitted_tail':valid-count,'stop_reason':reason}

class MemoryFile:
    def __init__(self,image,overlay,pages,decrypt):
        self.image=image;self.overlay=overlay;self.pages=pages;self.decrypt=decrypt;self.cache={};self.read_pages=set()
    def xRead(self,amount,offset):
        out=bytearray()
        while amount:
            number=offset//4096+1;start=offset%4096;count=min(amount,4096-start)
            if number>self.pages:out.extend(b'\0'*count)
            else:
                if number not in self.cache:
                    page=self.overlay.get(number,self.image[(number-1)*4096:number*4096])
                    self.cache[number]=self.decrypt(page,number);self.read_pages.add(number)
                out.extend(self.cache[number][start:start+count])
            offset+=count;amount-=count
        return bytes(out)
    def xFileSize(self):return self.pages*4096
    def xClose(self):self.cache.clear()
    def xLock(self,level):pass
    def xUnlock(self,level):pass
    def xCheckReservedLock(self):return False
    def xFileControl(self,op,pointer):return False
    def xSectorSize(self):return 4096
    def xDeviceCharacteristics(self):return apsw.SQLITE_IOCAP_IMMUTABLE
    def xWrite(self,*args):raise apsw.ReadOnlyError('read only source')
    def xTruncate(self,*args):raise apsw.ReadOnlyError('read only source')
    def xSync(self,*args):pass

class MemoryVFS(apsw.VFS):
    def __init__(self,view):
        self.name='wk-readonly-'+uuid.uuid4().hex;self.view=view
        super().__init__(self.name,'')
    def xOpen(self,name,flags):
        if not flags[0]&apsw.SQLITE_OPEN_MAIN_DB or flags[0]&apsw.SQLITE_OPEN_READWRITE:raise apsw.ReadOnlyError('source writes denied')
        flags[1]=apsw.SQLITE_OPEN_READONLY;return self.view
    def xAccess(self,*args):return False
    def xDelete(self,*args):raise apsw.ReadOnlyError('source deletes denied')

def decode(value):
    if value is None:return ''
    if isinstance(value,str):return value
    if value.startswith(b'\x28\xb5\x2f\xfd'):value=zstandard.ZstdDecompressor().decompress(value,max_output_size=10*1024**2)
    return value.decode('utf-8')

def cached_voice_text(data):
    """Verified WeChat 4.1.15: packed field 5, status 1=2, UTF-8 text 2."""
    def fields(blob):
        pos=0;result={}
        def number():
            nonlocal pos
            value=0
            for shift in range(0,70,7):
                if pos>=len(blob):raise ValueError('truncated')
                b=blob[pos];pos+=1;value|=(b&127)<<shift
                if b<128:return value
            raise ValueError('varint')
        while pos<len(blob):
            key=number();wire=key&7;field=key>>3
            if not field:raise ValueError('field')
            if wire==0:value=number()
            elif wire==2:
                size=number();value=blob[pos:pos+size];pos+=size
                if len(value)!=size:raise ValueError('length')
            elif wire in (1,5):
                pos+=8 if wire==1 else 4
                if pos>len(blob):raise ValueError('length')
                continue
            else:raise ValueError('wire')
            result[field]=value
        return result
    try:
        outer=fields(data or b'');inner=fields(outer.get(5,b''))
        return inner.get(2,b'').decode('utf-8').strip() if inner.get(1)==2 else ''
    except (ValueError,TypeError,AttributeError,UnicodeDecodeError):return ''

def query_conversation(connection, identity, since, until, text_only=False):
    table='Msg_'+hashlib.md5(identity.encode()).hexdigest()
    allowed={table,'Name2Id','sqlite_master','sqlite_schema'}
    queries=[]
    def authorize(action,arg1,arg2,dbname,trigger):
        if action==apsw.SQLITE_READ:return apsw.SQLITE_OK if arg1 in allowed else apsw.SQLITE_DENY
        return apsw.SQLITE_OK if action in (apsw.SQLITE_SELECT,apsw.SQLITE_FUNCTION,apsw.SQLITE_TRANSACTION) else apsw.SQLITE_DENY
    connection.set_authorizer(authorize)
    if not connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone():return [],queries
    extra=',packed_info_data' if text_only else ''
    where=' AND (local_type & 65535) IN (1,34,49)' if text_only else ''
    sql='SELECT local_id,server_id,local_type,real_sender_id,create_time,message_content,compress_content'+extra+' FROM "'+table+'" WHERE create_time>=? AND create_time<?'+where+' ORDER BY create_time,local_id'
    audit={'table':table,'since':since,'until':until,'voice_pending':0,'voice_transcripts':0,'text_cards':0}
    if text_only:audit['type_counts']={str(t):n for t,n in connection.execute('SELECT (local_type & 65535),count(*) FROM "'+table+'" WHERE create_time>=? AND create_time<? GROUP BY (local_type & 65535)',(since,until))}
    queries.append(audit)
    rows=[];senders={}
    for row in connection.execute(sql,(since,until)):
        typ=row[2]&65535;mode='visible_text'
        if text_only and typ==34:
            body=cached_voice_text(row[7])
            if not body:audit['voice_pending']+=1;continue
            audit['voice_transcripts']+=1;mode='wechat_cached_voice_transcript'
        else:body=decode(row[5]) or decode(row[6])
        if text_only and typ==49:
            import re,xml.etree.ElementTree as ET
            try:
                root=ET.fromstring(re.sub(r'^[^\s<>:]{1,100}:\n','',body))
                app=root if root.tag=='appmsg' else root.find('.//appmsg')
                if app is None or app.findtext('type') not in ('1','57'):continue
                body=app.findtext('title') or ''
                if not body.strip():continue
                audit['text_cards']+=1;mode='wechat_text_quote'
            except ET.ParseError:continue
        sender=row[3]
        if sender not in senders:
            found=connection.execute('SELECT user_name FROM Name2Id WHERE rowid=?',(sender,)).fetchone()
            senders[sender]=found[0] if found else ''
        rows.append({'local_id':row[0],'server_id':row[1],'message_type':row[2],'sender_id':senders[sender],'timestamp':row[4],
                     'body':body,'content_mode':mode})
    return rows,queries

class WeChatCollector:
    def __init__(self,raw):self.raw=raw
    def collect(self,run):
        s=self.raw.source(run['source']['id'],True)
        if s['platform']!='wechat' or s['kind']!='chat':raise StoreError('未批准的微信会话。',403)
        since=int(dt.datetime.fromisoformat(run['since']).timestamp());until=int(dt.datetime.fromisoformat(run['until']).timestamp())
        with _guard:
            config = require_wechat()
            account = config['account_id']
            owner_name = settings().get('display_name', '我')
            paths=sorted((Path(config['database_dir']) / 'message').glob('message_[0-9]*.db'))
            if not paths:raise WeChatReadError('未找到已配置的本机微信消息库。',503)
            helper=crypto_helper();readability=ensure_keys(helper,paths)
            records={};receipts=[]
            for path in paths:
                image,log,receipt=stable_image(path)
                overlay,pages,wal=committed_wal(log);key=_keys[str(path)];mkey=helper.mac_key(key,image[:4096])
                def decrypt(page,pgno):
                    if len(page)!=4096 or not helper.authenticated(page,pgno,mkey):raise StoreError('微信查询页校验失败；本轮未提交。')
                    d=Cipher(algorithms.AES(key),modes.CBC(page[-80:-64])).decryptor()
                    return (b'SQLite format 3\0' if pgno==1 else b'')+d.update(page[16 if pgno==1 else 0:-80])+d.finalize()+b'\0'*80
                view=MemoryFile(image,overlay,pages or len(image)//4096,decrypt);vfs=MemoryVFS(view)
                connection=None
                try:
                    connection=apsw.Connection('file:work-chat?immutable=1',flags=apsw.SQLITE_OPEN_READONLY|apsw.SQLITE_OPEN_URI,vfs=vfs.name)
                    rows,queries=query_conversation(connection,s['identity'],since,until,s['config'].get('text_only',False))
                    for m in rows:
                        body=visible_text(m['body'],'wechat')
                        topics=s['config'].get('topics')
                        if topics and not any(t in body for t in topics):continue
                        server=str(m['server_id'] or '')
                        oid='wechat:'+account+':'+s['identity']+':'+('server:'+server if server and server!='0' else path.name+':local:'+str(m['local_id']))
                        r={'source_id':s['id'],'object_id':oid,'kind':'message','body':body,'title':s['name'],'revision':'','url':'',
                           'event_at':event_time(m['timestamp']),'sender':owner_name if m['sender_id']==account else m['sender_id'],
                           'sender_id':m['sender_id'],'message_type':str(m['message_type']),'content_mode':m['content_mode']}
                        if oid in records and records[oid]!=r:raise StoreError('微信分片出现冲突消息，请重试。')
                        records[oid]=r
                    receipt.update(wal=wal,queried=queries,pages_decrypted=len(view.read_pages),plaintext_database_saved=False)
                    receipts.append(receipt)
                finally:
                    if connection:connection.close()
                    vfs.unregister()
                del image,log,overlay,view
        found=any(r['queried'] for r in receipts)
        return list(records.values()),{'complete':True,'local_table_found':found,'coverage':'本机名单内指定会话'+('，限定项目主题' if s['config'].get('topics') else '')+('；本机未找到会话表，不能认定无历史' if not found else ''),
             'account':account,'databases':receipts,'keys_saved':False,'contact_database_opened':False,'source_open_mode':'rb','readability':readability}
