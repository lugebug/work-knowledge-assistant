"""Append-only business-source versions; source sync never writes business overlays."""
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import uuid
from store import StoreError, now, json_text
from work_sources import scope

from runtime_config import history_since, owner_account_ids, settings
TOPICS = []

def digest(value):
    return hashlib.sha256((value if isinstance(value, bytes) else json_text(value).encode('utf-8'))).hexdigest()

def source_key(platform, kind, identity):
    return f'{platform}:{kind}:{identity}'

def schedule_status(conn):
    """Read the app's current saved automation state; no shadow scheduler."""
    import tomllib
    row=conn.execute("SELECT value FROM wk_meta WHERE key='periodic-v21'").fetchone()
    if not row:return {'active':False,'description':'尚未启用定期更新'}
    config=json.loads(row[0]);active=[]
    for aid in config.get('automation_ids',[]):
        path=Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')/'automations'/aid/'automation.toml'
        try:
            entry=tomllib.loads(path.read_text('utf-8'))
            if entry.get('status')=='ACTIVE':active.append(aid)
        except (OSError,ValueError):pass
    valid=not config.get('trial_end') or dt.datetime.fromisoformat(now())<=dt.datetime.fromisoformat(config['trial_end'])
    return {'active':bool(active) and valid,'active_ids':active if valid else [],
            'description':config['description'] if active and valid else '定期更新尚未运行、已暂停或已到所设截止时间',
            'trial_end':config.get('trial_end')}

class RawStore:
    def __init__(self, store):
        self.store = store
        self.root = store.path.parent / 'raw'

    def initialize(self):
        with self.store.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS raw_sources(
              id TEXT PRIMARY KEY, platform TEXT NOT NULL, kind TEXT NOT NULL, identity TEXT NOT NULL,
              name TEXT NOT NULL, state TEXT NOT NULL, config TEXT NOT NULL, basis TEXT NOT NULL,
              revision INTEGER NOT NULL DEFAULT 1, added_at TEXT NOT NULL,
              UNIQUE(platform,kind,identity));
            CREATE TABLE IF NOT EXISTS raw_source_history(
              id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, before_json TEXT NOT NULL,
              after_json TEXT NOT NULL, actor TEXT NOT NULL, happened_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS raw_runs(
              id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES raw_sources(id),
              state TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT,
              since_at TEXT NOT NULL, until_at TEXT NOT NULL, source_revision INTEGER NOT NULL,
              retry_of TEXT, result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
              manifest TEXT, worker_pid INTEGER NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS raw_one_running ON raw_runs(source_id) WHERE state='running';
            CREATE TABLE IF NOT EXISTS raw_sync(
              source_id TEXT PRIMARY KEY REFERENCES raw_sources(id), covered_until TEXT NOT NULL,
              success_at TEXT NOT NULL, run_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS raw_versions(
              id TEXT PRIMARY KEY, source_id TEXT NOT NULL REFERENCES raw_sources(id),
              object_id TEXT NOT NULL, object_kind TEXT NOT NULL, payload TEXT NOT NULL,
              sha256 TEXT NOT NULL, observed_at TEXT NOT NULL, file_path TEXT NOT NULL,
              UNIQUE(source_id,object_id,sha256));
            CREATE TABLE IF NOT EXISTS raw_heads(
              source_id TEXT NOT NULL, object_id TEXT NOT NULL, version_id TEXT NOT NULL REFERENCES raw_versions(id),
              PRIMARY KEY(source_id,object_id));
            CREATE TABLE IF NOT EXISTS raw_changes(
              id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES raw_runs(id),
              source_id TEXT NOT NULL, object_id TEXT NOT NULL, version_id TEXT NOT NULL REFERENCES raw_versions(id),
              previous_version_id TEXT, change_type TEXT NOT NULL, reviewed_at TEXT);
            CREATE TRIGGER IF NOT EXISTS raw_versions_no_update BEFORE UPDATE ON raw_versions BEGIN SELECT RAISE(ABORT,'raw version is immutable'); END;
            CREATE TRIGGER IF NOT EXISTS raw_versions_no_delete BEFORE DELETE ON raw_versions BEGIN SELECT RAISE(ABORT,'raw version is immutable'); END;
            ''')
            for s in scope()['conversations']:
                identity=s['id']; key=source_key(s['platform'],'chat',identity)
                config={'projects':s['projects'],'since':history_since(),'overlap_hours':72}
                c.execute('INSERT OR IGNORE INTO raw_sources VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (key,s['platform'],'chat',identity,s['name'],'approved',json_text(config),json_text({'note':s['basis'],'origin':'已确认工作范围'}),1,now()))
            for did in scope()['documents']:
                if not did.startswith('feishu:'): continue
                row=c.execute('SELECT title,url FROM documents WHERE id=?',(did,)).fetchone()
                if not row: continue
                identity=did.split(':')[1]
                c.execute('INSERT OR IGNORE INTO raw_sources VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (source_key('feishu','document',identity),'feishu','document',identity,row['title'],'approved',
                     json_text({'url':row['url'],'since':history_since(),'overlap_hours':72}),
                     json_text({'note':'已确认工作文档；授权稳定文档身份，保存各修订版本','legacy_id':did}),1,now()))

    @staticmethod
    def _decode(row):
        result=dict(row)
        for key in ('config','basis'):
            result[key]=json.loads(result[key])
        return result

    def source(self, sid, approved=False, conn=None):
        if conn is None:
            with self.store.connect() as c: return self.source(sid,approved,c)
        row=conn.execute('SELECT * FROM raw_sources WHERE id=?',(sid,)).fetchone()
        if not row or approved and row['state']!='approved':
            raise StoreError('来源未批准或已暂停；未读取正文。',403)
        return self._decode(row)

    def overview(self):
        with self.store.connect() as c:
            from source_health import in_connection as source_health
            sources=[]
            for row in c.execute('SELECT * FROM raw_sources ORDER BY platform,kind,name'):
                s=self._decode(row)
                sync=c.execute('SELECT * FROM raw_sync WHERE source_id=?',(s['id'],)).fetchone()
                latest=c.execute('SELECT * FROM raw_runs WHERE source_id=? ORDER BY started_at DESC,rowid DESC LIMIT 1',(s['id'],)).fetchone()
                s['sync']=dict(sync) if sync else None
                s['latest_run']=self._run_dict(latest) if latest else None
                s['objects']=c.execute('SELECT count(*) FROM raw_heads WHERE source_id=?',(s['id'],)).fetchone()[0]
                sources.append(s)
            task_discovery=c.execute("SELECT value FROM wk_meta WHERE key='feishu_tasks_discovery_v36'").fetchone()
            return {'sources':sources,'tasks_discovery':json.loads(task_discovery[0]) if task_discovery else None,'runs':[self._run_dict(r) for r in c.execute('SELECT * FROM raw_runs ORDER BY started_at DESC,rowid DESC LIMIT 30')],
                'pending_changes':c.execute('SELECT count(*) FROM raw_changes WHERE reviewed_at IS NULL').fetchone()[0],
                'versions':c.execute('SELECT count(*) FROM raw_versions').fetchone()[0],
                'storage':str(self.root),'automatic_schedule':schedule_status(c)['active'],'schedule':schedule_status(c),
                'health':source_health(c)}

    @staticmethod
    def _run_dict(row):
        d=dict(row);d['result']=json.loads(d['result']);return d

    def register_work_chat(self, platform, identity, name, basis, since, actor='用户授权，Codex依据整理'):
        """Register a reviewed work identity, with durable authorization provenance."""
        import re
        if platform not in ('wechat','feishu') or not re.fullmatch(r'[A-Za-z0-9_@.-]{3,100}',identity):
            raise StoreError('工作会话身份无效。')
        if not name.strip() or not isinstance(basis,dict) or not basis.get('note') or not basis.get('evidence'):
            raise StoreError('需要工作关系依据和准确身份。')
        start=dt.datetime.fromisoformat(since)
        if start.tzinfo is None:raise StoreError('起始时间必须带时区。')
        sid=source_key(platform,'chat',identity)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM raw_sources WHERE id=?',(sid,)).fetchone()
            before=self._decode(old) if old else {}
            if before and before['state'] in ('paused','rejected','candidate'):
                raise StoreError('已暂停、拒绝或待判断的来源不可批量重开。',403)
            config=dict(before.get('config',{}),since=start.isoformat(timespec='seconds'),overlap_hours=72,text_only=True)
            config.pop('topics',None)
            if before and before['config']==config:return before
            if before:
                c.execute('UPDATE raw_sources SET config=?,basis=?,revision=revision+1 WHERE id=?',(json_text(config),json_text(dict(basis,previous_basis=before['basis'])),sid))
            else:
                c.execute('INSERT INTO raw_sources VALUES(?,?,?,?,?,?,?,?,?,?)',(sid,platform,'chat',identity,name,'approved',json_text(config),json_text(basis),1,now()))
            after=self.source(sid,True,c)
            c.execute('INSERT INTO raw_source_history(source_id,before_json,after_json,actor,happened_at) VALUES(?,?,?,?,?)',(sid,json_text(before),json_text(after),actor,now()))
            return after

    def exclude_chat(self, platform, identity, name, basis, actor='用户明确排除'):
        """Persist an explicit exclusion without ever admitting or reading the chat."""
        import re
        if platform not in ('wechat','feishu') or not re.fullmatch(r'[A-Za-z0-9_@.-]{3,100}',identity) or not name.strip():
            raise StoreError('需要准确会话身份和名称。')
        if not isinstance(basis,dict) or not basis.get('note') or not basis.get('evidence'):
            raise StoreError('排除会话需要用户判断依据。')
        sid=source_key(platform,'chat',identity)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM raw_sources WHERE id=?',(sid,)).fetchone()
            before=self._decode(old) if old else {}
            if before.get('state')=='rejected':return before
            if before:
                c.execute("UPDATE raw_sources SET state='rejected',basis=?,revision=revision+1 WHERE id=?",(json_text(dict(basis,previous_basis=before['basis'])),sid))
            else:
                config={'since':now(),'overlap_hours':72,'text_only':True}
                c.execute('INSERT INTO raw_sources VALUES(?,?,?,?,?,?,?,?,?,?)',(sid,platform,'chat',identity,name,'rejected',json_text(config),json_text(basis),1,now()))
            after=self.source(sid,False,c)
            c.execute('INSERT INTO raw_source_history(source_id,before_json,after_json,actor,happened_at) VALUES(?,?,?,?,?)',(sid,json_text(before),json_text(after),actor,now()))
            return after

    def register_work_document(self, identity, name, url, basis, since=history_since(), metadata=None):
        """Admit a visible text document without reopening a protected source."""
        import re
        from urllib.parse import urlparse
        if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9]{10,100}',identity):
            raise StoreError('飞书文档身份无效。')
        parsed=urlparse(url)
        paths=('/docx/'+identity,'/docs/'+identity)
        if isinstance(metadata,dict) and metadata.get('document_role')=='minute_artifacts':
            if metadata.get('minute_token')!=identity:raise StoreError('妙记文字来源身份不一致。')
            paths=('/minutes/'+identity,)
        if parsed.scheme!='https' or not (parsed.hostname or '').endswith(('.feishu.cn','.larksuite.com','.larkoffice.com')) or parsed.path not in paths:
            raise StoreError('需要与文档身份相符的飞书链接。')
        if not isinstance(name,str) or not name.strip() or not isinstance(basis,dict) or not basis.get('note') or not basis.get('evidence'):
            raise StoreError('需要文档名称和明确的工作来源依据。')
        start=dt.datetime.fromisoformat(since)
        if start.tzinfo is None or start<dt.datetime(2026,9,1,tzinfo=start.tzinfo):
            raise StoreError('文档发现起点不得早于当前9月1日阶段，且必须带时区。')
        if metadata is not None and not isinstance(metadata,dict):raise StoreError('文档目录信息格式不正确。')
        sid=source_key('feishu','document',identity)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM raw_sources WHERE id=?',(sid,)).fetchone()
            if old:
                # Do not replace user-maintained names, scope, pause or rejection.
                return dict(self._decode(old),unchanged=True,protected=old['state']!='approved')
            config=dict(url=url,since=start.isoformat(timespec='seconds'),overlap_hours=72,text_only=True,
                        discovery_metadata=metadata or {})
            c.execute('INSERT INTO raw_sources VALUES(?,?,?,?,?,?,?,?,?,?)',
                (sid,'feishu','document',identity,name.strip(),'approved',json_text(config),json_text(basis),1,now()))
            after=self.source(sid,True,c)
            c.execute('INSERT INTO raw_source_history(source_id,before_json,after_json,actor,happened_at) VALUES(?,?,?,?,?)',
                (sid,'{}',json_text(after),'Codex核对飞书文档目录',now()))
            return after

    def register_work_task(self, identity, name, url, basis, since=history_since(), metadata=None):
        from feishu_tasks import guid, SINCE
        from urllib.parse import urlparse, parse_qs
        identity = guid(identity)
        parsed = urlparse(url)
        if (parsed.scheme != 'https' or parsed.hostname not in ('applink.feishu.cn','applink.larksuite.com')
                or parsed.path not in ('/client/todo/detail','/client/todo/task')
                or parse_qs(parsed.query).get('guid') != [identity]):
            raise StoreError('任务链接与飞书任务身份不符。')
        start = dt.datetime.fromisoformat(since)
        if start.tzinfo is None or start < dt.datetime.fromisoformat(SINCE):
            raise StoreError('任务历史范围不得早于9月1日；仍未完成的旧任务只保留当前快照。')
        if not isinstance(name, str) or not name.strip() or len(name)>3000 or not isinstance(basis, dict) or not basis.get('note') or not basis.get('evidence'):
            raise StoreError('需要任务名称和可核对的工作来源依据。')
        sid = source_key('feishu','task',identity)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT * FROM raw_sources WHERE id=?',(sid,)).fetchone()
            if old:
                return dict(self._decode(old),unchanged=True,protected=old['state']!='approved')
            config = dict(url=url,since=start.isoformat(timespec='seconds'),overlap_hours=72,text_only=True,
                current_open_older=True,snapshot_only=True,discovery_metadata=metadata or {})
            c.execute('INSERT INTO raw_sources VALUES(?,?,?,?,?,?,?,?,?,?)',
                (sid,'feishu','task',identity,name.strip(),'approved',json_text(config),json_text(basis),1,now()))
            after = self.source(sid,True,c)
            c.execute('INSERT INTO raw_source_history(source_id,before_json,after_json,actor,happened_at) VALUES(?,?,?,?,?)',
                (sid,'{}',json_text(after),'Codex核对飞书任务',now()))
            return after

    def begin(self, sid, retry_of=None, since_at=None, until_at=None):
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            source=self.source(sid,True,c)
            if c.execute("SELECT 1 FROM raw_runs WHERE source_id=? AND state='running'",(sid,)).fetchone():
                raise StoreError('该来源正在更新，请等待结束。',409)
            sync=c.execute('SELECT covered_until FROM raw_sync WHERE source_id=?',(sid,)).fetchone()
            until=now()
            since=source['config']['since']
            if sync:
                since=max(since,(dt.datetime.fromisoformat(sync[0])-dt.timedelta(hours=source['config'].get('overlap_hours',72))).isoformat(timespec='seconds'))
            if retry_of:
                prior=c.execute('SELECT * FROM raw_runs WHERE id=? AND source_id=?',(retry_of,sid)).fetchone()
                if not prior or prior['state'] not in ('failed','interrupted'):
                    raise StoreError('只能重试该来源的失败或中断批次。')
                since=min(since,prior['since_at'])
            if since_at is not None or until_at is not None:
                if not since_at or not until_at or retry_of:raise StoreError('历史补录需要完整时间窗，不能同时重试。')
                start,end=dt.datetime.fromisoformat(since_at),dt.datetime.fromisoformat(until_at)
                if start.tzinfo is None or end.tzinfo is None or not dt.timedelta(0)<end-start<=dt.timedelta(days=93):
                    raise StoreError('历史补录时间窗需为1秒至93天，并带时区。')
                if start<dt.datetime.fromisoformat(source['config']['since']) or end>dt.datetime.fromisoformat(now()):
                    raise StoreError('历史补录超出已批准时间范围。')
                since,until=start.isoformat(timespec='seconds'),end.isoformat(timespec='seconds')
            rid=uuid.uuid4().hex
            c.execute('INSERT INTO raw_runs(id,source_id,state,started_at,since_at,until_at,source_revision,retry_of,worker_pid) VALUES(?,?,?,?,?,?,?,?,?)',
                (rid,sid,'running',now(),since,until,source['revision'],retry_of,os.getpid()))
            return dict(id=rid,source=source,since=since,until=until)

    def fail(self, rid, reason):
        with self.store.connect() as c:
            c.execute("UPDATE raw_runs SET state='failed',finished_at=?,error=? WHERE id=? AND state='running'",(now(),reason[:600],rid))

    def recover_interrupted(self):
        # Called once by a new server/CLI process. Never guesses by age alone.
        import ctypes
        def alive(pid):
            if pid==os.getpid(): return True
            if os.name=='nt':
                k=ctypes.WinDLL('kernel32',use_last_error=True)
                k.OpenProcess.restype=ctypes.c_void_p
                handle=k.OpenProcess(0x1000,False,pid)
                if not handle:return ctypes.get_last_error()!=87
                k.CloseHandle.argtypes=[ctypes.c_void_p];k.CloseHandle(handle);return True
            try:os.kill(pid,0);return True
            except ProcessLookupError:return False
            except PermissionError:return True
        with self.store.connect() as c:
            for row in c.execute("SELECT id,worker_pid FROM raw_runs WHERE state='running'").fetchall():
                if not alive(row['worker_pid']):
                    c.execute("UPDATE raw_runs SET state='interrupted',finished_at=?,error=? WHERE id=? AND state='running'",(now(),'上次采集进程已退出；完成位置未推进，可重试。',row['id']))

    def commit(self, run, records, receipt):
        """All pages must be collected before here. Files may be orphaned on crash; DB never points at a partial file."""
        if receipt.get('complete') is not True:raise StoreError('未确认完整读取，禁止推进完成位置。')
        sid=run['source']['id'];stamp=now();unique={}
        for record in records:
            if record.get('source_id')!=sid or record.get('kind') not in ('message','document','task') or not record.get('object_id') or not isinstance(record.get('body'),str):
                raise StoreError('采集返回范围或格式错误；批次未提交。')
            if record['object_id'] in unique and unique[record['object_id']]!=record:
                raise StoreError('同一批次出现冲突版本，请重试稳定读取。')
            unique[record['object_id']]=record
        source_dir=self.root/digest(sid)[:20]
        version_dir=source_dir/'versions';version_dir.mkdir(parents=True,exist_ok=True)
        prepared=[]
        for record in unique.values():
            encoded=json_text(record).encode('utf-8');sha=digest(encoded);vid='raw:'+digest([sid,record['object_id'],sha])
            path=version_dir/(sha+'.json')
            self._immutable_file(path,encoded)
            prepared.append((vid,record,sha,str(path)))
        manifest_path=source_dir/'batches'/(run['id']+'.json')
        manifest_path.parent.mkdir(exist_ok=True)
        manifest={'run_id':run['id'],'source_id':sid,'since':run['since'],'until':run['until'],'versions':[p[0] for p in prepared],'receipt':receipt}
        self._immutable_file(manifest_path,json_text(manifest).encode('utf-8'))
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            source=self.source(sid,True,c)
            if source['revision']!=run['source']['revision']:
                raise StoreError('采集中来源范围发生变化，未提交；请按新范围重试。',409)
            active=c.execute('SELECT state FROM raw_runs WHERE id=?',(run['id'],)).fetchone()
            if not active or active[0]!='running':raise StoreError('批次已结束。',409)
            added=changed=unchanged=0
            for vid,record,sha,path in prepared:
                old=c.execute('SELECT version_id FROM raw_heads WHERE source_id=? AND object_id=?',(sid,record['object_id'])).fetchone()
                c.execute('INSERT OR IGNORE INTO raw_versions VALUES(?,?,?,?,?,?,?,?)',(vid,sid,record['object_id'],record['kind'],json_text(record),sha,stamp,path))
                if old and old[0]==vid:unchanged+=1;continue
                c.execute('INSERT INTO raw_heads VALUES(?,?,?) ON CONFLICT(source_id,object_id) DO UPDATE SET version_id=excluded.version_id',(sid,record['object_id'],vid))
                c.execute('INSERT INTO raw_changes(run_id,source_id,object_id,version_id,previous_version_id,change_type) VALUES(?,?,?,?,?,?)',(run['id'],sid,record['object_id'],vid,old[0] if old else None,'changed' if old else 'new'))
                if old:changed+=1
                else:added+=1
            result={'new':added,'changed':changed,'unchanged':unchanged,'records':len(prepared),'receipt':receipt}
            c.execute("UPDATE raw_runs SET state='succeeded',finished_at=?,result=?,manifest=? WHERE id=?",(stamp,json_text(result),str(manifest_path),run['id']))
            if receipt.get('local_table_found') is not False:
                c.execute('INSERT INTO raw_sync VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET covered_until=excluded.covered_until,success_at=excluded.success_at,run_id=excluded.run_id WHERE excluded.covered_until>=raw_sync.covered_until',(sid,run['until'],stamp,run['id']))
            return dict(run_id=run['id'],state='succeeded',**result)

    @staticmethod
    def _immutable_file(path, content):
        if path.exists():
            if path.read_bytes()!=content:raise StoreError('已有原始版本校验不符，拒绝覆盖。')
            return
        temp=path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        with temp.open('xb') as f:
            f.write(content);f.flush();os.fsync(f.fileno())
        # Version paths are content-addressed; all writers for a source are serialized.
        os.replace(temp,path)

    def version(self, vid):
        with self.store.connect() as c:
            pointer=c.execute('SELECT source_id FROM raw_versions WHERE id=?',(vid,)).fetchone()
            if not pointer:raise StoreError('原始版本不存在。',404)
            self.source(pointer[0],True,c)
            row=c.execute('SELECT * FROM raw_versions WHERE id=?',(vid,)).fetchone()
            result=dict(row);result['payload']=json.loads(result['payload']);return result

    def changes(self, limit=100):
        if not 1<=limit<=200:raise StoreError('变化清单上限为200条。')
        with self.store.connect() as c:
            return [dict(r) for r in c.execute('''SELECT x.id,x.run_id,x.source_id,x.object_id,x.version_id,x.change_type,x.reviewed_at,s.name
              FROM raw_changes x JOIN raw_sources s ON s.id=x.source_id WHERE s.state='approved' ORDER BY x.id DESC LIMIT ?''',(limit,))]

    def decide_source(self, sid, state, revision, actor='用户'):
        if state not in ('approved','paused','rejected'):raise StoreError('不支持的来源状态。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE');before=self.source(sid,False,c)
            if before['revision']!=revision:raise StoreError('来源名单已更新，请刷新。',409)
            if before['state']==state:return {'unchanged':True}
            c.execute('UPDATE raw_sources SET state=?,revision=revision+1 WHERE id=?',(state,sid))
            after=self.source(sid,False,c)
            c.execute('INSERT INTO raw_source_history(source_id,before_json,after_json,actor,happened_at) VALUES(?,?,?,?,?)',(sid,json_text(before),json_text(after),actor,now()))
            return after

    def propose_wechat(self, identity, name, evidence, projects, explicit_work=False):
        """Codex maintains the list from already approved evidence; no contact enumeration."""
        import re
        from work_sources import resolve_reference
        if not re.fullmatch(r'[A-Za-z0-9_@.-]{3,100}',identity) or not name.strip():raise StoreError('需要准确微信账号/群标识和名称。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            refs=[resolve_reference(c,ref) for ref in evidence]
            if not refs:raise StoreError('候选必须有已有工作来源依据。')
            if not isinstance(projects,list) or not projects or any(p not in self.store._records(c,'projects') for p in projects):raise StoreError('需要关联现有项目。')
            # Automatic admission requires exact identity AND explicit work relation in quoted evidence.
            def own_statement(ref):
                if ref['type']=='raw':
                    payload=json.loads(c.execute('SELECT payload FROM raw_versions WHERE id=?',(ref['id'],)).fetchone()[0])
                    return payload.get('sender_id') in owner_account_ids()
                if ref['type']=='message':
                    row=c.execute('SELECT sender_id FROM messages WHERE id=?',(ref['id'],)).fetchone()
                    return row and row[0] in owner_account_ids()
                return False
            decisive=any(own_statement(r) and identity in r['quote'] and re.search(r'项目工作群|工作对接群|项目对接人|客户服务群',r['quote']) for r in refs)
            state='approved' if explicit_work and decisive else 'candidate'
            sid=source_key('wechat','chat',identity)
            old=c.execute('SELECT * FROM raw_sources WHERE id=?',(sid,)).fetchone()
            if old:return {'source':self._decode(old),'unchanged':True}
            config={'projects':projects,'since':now(),'overlap_hours':72}
            basis={'note':'由已批准工作来源发现；不读取候选会话正文','sources':refs,'automatic_admission':state=='approved'}
            c.execute('INSERT INTO raw_sources VALUES(?,?,?,?,?,?,?,?,?,?)',(sid,'wechat','chat',identity,name,state,json_text(config),json_text(basis),1,now()))
            c.execute('INSERT INTO raw_source_history(source_id,before_json,after_json,actor,happened_at) VALUES(?,?,?,?,?)',(sid,'{}',json_text(self.source(sid,False,c)),'Codex依据整理',now()))
            return {'source':self.source(sid,False,c),'needs_decision':state=='candidate'}

    def discover_wechat_candidates(self, run_id):
        """Discover exact identifiers in newly saved work evidence, never enumerate contacts."""
        import re
        candidates=[]
        with self.store.connect() as c:
            rows=c.execute('''SELECT v.id,v.payload,s.config FROM raw_changes x JOIN raw_versions v ON v.id=x.version_id
              JOIN raw_sources s ON s.id=x.source_id WHERE x.run_id=? AND s.state='approved' ''',(run_id,)).fetchall()
        for row in rows:
            p=json.loads(row['payload']);body=p['body'];projects=json.loads(row['config']).get('projects',[])
            if not projects:continue
            for match in re.finditer(r'(?<![A-Za-z0-9_])(?:wxid_[A-Za-z0-9_]{5,80}|[0-9]{5,25}@chatroom)(?![A-Za-z0-9_])',body):
                identity=match[0]
                if identity in owner_account_ids():continue
                quote=body[max(0,match.start()-100):min(len(body),match.end()+100)]
                if not re.search(r'工作|项目|客户|对接',quote):continue
                evidence=[dict(type='raw',id=row['id'],quote=quote)]
                candidates.append(self.propose_wechat(identity,'待核对：'+identity,evidence,projects,explicit_work=True))
        return candidates
