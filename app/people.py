"""Evidence-backed person dossiers built on the existing people/account records.

Account identity lives in people after V28; contacts is a source compatibility view.
Review never expands collection scope. Original tables and human fields stay intact.
"""
import hashlib
import json
import datetime as dt
from store import StoreError, now, json_text
from work_sources import resolve_reference

RELATION_STATES = {'supported':'资料支持','pending':'待你确认','confirmed':'你已确认','rejected':'你已否定'}
RELATION_FIELDS = {'label','detail','target_id','represented_org_id','department_id','phase','valid_from','valid_to'}
RELATION_EXTRAS = ('represented_org_id','department_id','phase','valid_from','valid_to')

def stable_id(prefix, *parts):
    return prefix+'-'+hashlib.sha256(json_text(parts).encode()).hexdigest()[:24]

class People:
    def __init__(self, store): self.store=store

    def initialize(self):
        with self.store.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS wk_person_sources(
              id TEXT PRIMARY KEY,title TEXT NOT NULL,body TEXT NOT NULL,origin TEXT NOT NULL,observed_at TEXT NOT NULL);
            CREATE TRIGGER IF NOT EXISTS person_source_immutable_update BEFORE UPDATE ON wk_person_sources BEGIN SELECT RAISE(ABORT,'immutable person source'); END;
            CREATE TRIGGER IF NOT EXISTS person_source_immutable_delete BEFORE DELETE ON wk_person_sources BEGIN SELECT RAISE(ABORT,'immutable person source'); END;
            CREATE TABLE IF NOT EXISTS wk_person_relations(
              id TEXT PRIMARY KEY,person_id TEXT NOT NULL,kind TEXT NOT NULL,target_id TEXT NOT NULL,
              label TEXT NOT NULL,detail TEXT NOT NULL,state TEXT NOT NULL,basis TEXT NOT NULL,
              sources TEXT NOT NULL,revision INTEGER NOT NULL,updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wk_person_events(
              id INTEGER PRIMARY KEY,person_id TEXT NOT NULL,relation_id TEXT NOT NULL,action TEXT NOT NULL,
              before_json TEXT NOT NULL,after_json TEXT NOT NULL,actor TEXT NOT NULL,happened_at TEXT NOT NULL);
            ''')
            columns={r[1] for r in c.execute('PRAGMA table_info(wk_person_relations)')}
            for name in RELATION_EXTRAS:
                if name not in columns:c.execute('ALTER TABLE wk_person_relations ADD COLUMN '+name+" TEXT NOT NULL DEFAULT ''")

    @staticmethod
    def exists(c):
        return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='wk_person_relations'").fetchone())

    @staticmethod
    def capture(c,title,body,origin):
        """Only called by reviewed local importers, never exposed as a write API."""
        sid=stable_id('person-source',title,body,origin)
        c.execute('INSERT OR IGNORE INTO wk_person_sources VALUES(?,?,?,?,?)',(sid,title,body,origin,now()))
        return sid

    @staticmethod
    def _refs(c,refs):
        if not isinstance(refs,list) or not 1<=len(refs)<=8:raise StoreError('需提供1至8条工作资料依据。')
        try:return [resolve_reference(c,{k:r[k] for k in ('type','id','quote')}) for r in refs]
        except (ValueError,KeyError,TypeError) as e:raise StoreError('依据无效：'+str(e)) from None

    def seed_record(self,c,kind,rid,payload):
        if kind not in ('people','contacts'):raise StoreError('只能整理人员和工作账号。')
        current=self.store._records(c,kind,True)
        if rid in current:return False
        payload=dict(payload,_dossier_sources=self._refs(c,payload.get('_dossier_sources')))
        from model import FIELDS
        self.store._validate(c,kind,{k:v for k,v in payload.items() if k in FIELDS[kind]},True)
        if kind=='contacts':
            if not payload.get('account_id') or payload.get('platform') not in ('feishu','wechat'):raise StoreError('工作账号身份缺失。')
            if any(r.get('platform')==payload['platform'] and r.get('account_id')==payload['account_id'] for r in current.values()):
                raise StoreError('此平台账号已存在，不能重复建档。')
        c.execute('INSERT INTO wk_catalog VALUES(?,?,?)',(kind,rid,json_text(payload)))
        from work_model import enabled, Accounts
        if kind == 'contacts' and enabled(c):
            Accounts(self.store).seed(c, dict(payload, id=rid))
        person=rid if kind=='people' else payload.get('person_id') or payload.get('candidate_person_id') or ''
        self._event(c,person,rid,'资料建档',{},dict(kind=kind,id=rid,**payload),'Codex依据整理')
        return True

    @staticmethod
    def _decode(row):
        d=dict(row);d['sources']=json.loads(d['sources']);return d

    @staticmethod
    def _event(c,pid,rid,action,before,after,actor):
        c.execute('INSERT INTO wk_person_events(person_id,relation_id,action,before_json,after_json,actor,happened_at) VALUES(?,?,?,?,?,?,?)',
            (pid,rid,action,json_text(before),json_text(after),actor,now()))

    def _validate_relation(self,c,entry):
        if entry.get('kind') not in ('projects','organizations','departments'):raise StoreError('关系只支持项目、组织或部门。')
        if entry.get('person_id') not in self.store._records(c,'people'):raise StoreError('人员不存在。')
        if entry.get('target_id') not in self.store._records(c,entry['kind']):raise StoreError('关联对象不存在。')
        for k,limit in [('label',120),('detail',2000),('basis',1000)]:
            if not isinstance(entry.get(k),str) or len(entry[k])>limit:raise StoreError('关系说明格式或长度不正确。')
        if not entry['label'].strip():raise StoreError('请填写关系或角色。')
        for key in RELATION_EXTRAS:
            value=entry.get(key,'')
            if not isinstance(value,str) or len(value)>160:raise StoreError('关系维度格式不正确。')
        for key in ('valid_from','valid_to'):
            if entry.get(key):
                try:dt.date.fromisoformat(entry[key])
                except ValueError:raise StoreError('起止日期格式应为年-月-日。') from None
        if entry.get('valid_from') and entry.get('valid_to') and entry['valid_from']>entry['valid_to']:raise StoreError('结束日期不能早于开始日期。')
        for key,kind in [('represented_org_id','organizations'),('department_id','departments')]:
            if entry.get(key) and entry[key] not in self.store._records(c,kind):raise StoreError('关系引用的组织或部门不存在。')
        if entry.get('department_id') and entry.get('represented_org_id'):
            department=self.store._record(c,'departments',entry['department_id'])
            if department.get('organization_id')!=entry['represented_org_id']:raise StoreError('代表部门必须属于所代表组织。')

    def seed_relation(self,c,entry):
        self._validate_relation(c,entry)
        if entry.get('state') not in ('supported','pending'):raise StoreError('依据整理不能冒充用户确认。')
        refs=self._refs(c,entry['sources'])
        rid=entry.get('id') or stable_id('person-relation',entry['person_id'],entry['kind'],entry['target_id'],entry['label'])
        previous=c.execute('SELECT * FROM wk_person_relations WHERE id=?',(rid,)).fetchone()
        if previous:
            # Imported relationships are append-only in this first version. A new
            # interpretation must be reviewed separately, never overwrite a decision.
            return {'id':rid,'unchanged':True,'protected':previous['state'] in ('confirmed','rejected')}
        stamp=now()
        c.execute('INSERT INTO wk_person_relations(id,person_id,kind,target_id,label,detail,state,basis,sources,revision,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (rid,entry['person_id'],entry['kind'],entry['target_id'],entry['label'],entry['detail'],entry['state'],entry['basis'],json_text(refs),1,stamp))
        self._save_extras(c,rid,entry)
        self._event(c,entry['person_id'],rid,'依据关联',{},dict(entry,id=rid,sources=refs,revision=1),'Codex依据整理')
        return {'id':rid,'revision':1}

    def review_relation(self,rid,revision,state,values=None):
        if state not in ('confirmed','rejected') or type(revision) is not int:raise StoreError('请选择确认或否定，并刷新记录版本。')
        values=values or {}
        if not isinstance(values,dict) or set(values)-RELATION_FIELDS:raise StoreError('修改内容不正确。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row=c.execute('SELECT * FROM wk_person_relations WHERE id=?',(rid,)).fetchone()
            if not row:raise StoreError('关系不存在。',404)
            before=self._decode(row)
            if before['revision']!=revision:raise StoreError('关系已更新，请重新读取后确认。',409)
            after=dict(before,**values);after['state']=state
            self._validate_relation(c,after)
            if all(after.get(k,'')==before.get(k,'') for k in {'state',*RELATION_FIELDS}):return {'unchanged':True}
            after.update(revision=revision+1,updated_at=now())
            c.execute('UPDATE wk_person_relations SET target_id=?,label=?,detail=?,state=?,revision=?,updated_at=? WHERE id=?',
                (after['target_id'],after['label'].strip(),after['detail'].strip(),state,after['revision'],after['updated_at'],rid))
            self._save_extras(c,rid,after)
            self._event(c,before['person_id'],rid,'确认关系' if state=='confirmed' else '否定关系',before,after,'用户')
            return after

    @staticmethod
    def _save_extras(c,rid,entry):
        c.execute('UPDATE wk_person_relations SET '+','.join(k+'=?' for k in RELATION_EXTRAS)+' WHERE id=?',
                  (*[entry.get(k,'').strip() for k in RELATION_EXTRAS],rid))

    def create_relation(self,entry):
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            return self.create_relation_in_transaction(c,entry)

    def create_relation_in_transaction(self,c,entry):
        """Same relationship validation and audit, usable in a review batch."""
        if not isinstance(entry,dict) or set(entry)-({'person_id','kind'}|RELATION_FIELDS):raise StoreError('新增关系内容不正确。')
        entry=dict(entry,detail=entry.get('detail',''),basis='用户维护的关系',state='confirmed',sources=[])
        self._validate_relation(c,entry)
        rid=stable_id('person-relation',entry['person_id'],entry['kind'],entry['target_id'],entry['label'],entry.get('phase',''),entry.get('valid_from',''))
        if c.execute('SELECT 1 FROM wk_person_relations WHERE id=?',(rid,)).fetchone():raise StoreError('这条关系已存在，请修改已有记录。',409)
        c.execute('INSERT INTO wk_person_relations(id,person_id,kind,target_id,label,detail,state,basis,sources,revision,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (rid,entry['person_id'],entry['kind'],entry['target_id'],entry['label'],entry['detail'],'confirmed',entry['basis'],'[]',1,now()))
        self._save_extras(c,rid,entry)
        self._event(c,entry['person_id'],rid,'新增关系',{},entry,'用户')
        return dict(id=rid,revision=1)

    def support_relation(self,rid,revision,values,refs,basis):
        """Promote reviewed source facts, never overwrite a human decision."""
        if not isinstance(values,dict) or set(values)-RELATION_FIELDS:raise StoreError('关系整理字段不正确。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            raw=c.execute('SELECT * FROM wk_person_relations WHERE id=?',(rid,)).fetchone()
            if not raw:raise StoreError('关系不存在。',404)
            before=self._decode(raw)
            if before['revision']!=revision:raise StoreError('关系已更新，请重新读取。',409)
            if before['state'] in ('confirmed','rejected'):return dict(id=rid,protected=True)
            after=dict(before,**values,state='supported',basis=basis,sources=self._refs(c,refs))
            self._validate_relation(c,after)
            if all(after.get(k,'')==before.get(k,'') for k in {*RELATION_FIELDS,'state','basis','sources'}):return dict(id=rid,unchanged=True)
            after.update(revision=revision+1,updated_at=now())
            c.execute('UPDATE wk_person_relations SET target_id=?,label=?,detail=?,state=?,basis=?,sources=?,revision=?,updated_at=? WHERE id=?',
                      (after['target_id'],after['label'],after['detail'],after['state'],basis,json_text(after['sources']),after['revision'],after['updated_at'],rid))
            self._save_extras(c,rid,after)
            self._event(c,before['person_id'],rid,'依据补齐关系',before,after,'Codex依据整理')
            return dict(id=rid,revision=after['revision'])

    def review_account(self,cid,revision,person_id,decision):
        if decision not in ('confirm','reject'):raise StoreError('不支持的账号决定。')
        # Uses the same revision/protection/audit logic as ordinary account edits.
        detail=self.store.detail('contacts',cid)['record']
        if detail.get('candidate_person_id')!=person_id:raise StoreError('该账号不是此人的待确认对应，请重新读取。',409)
        if decision=='confirm':
            if detail.get('person_id') and detail['person_id']!=person_id:raise StoreError('账号已有其他确认身份，请使用调整对应。',409)
            values={'person_id':person_id,'candidate_person_id':'','mapping_note':'用户在人员档案确认此账号对应；原始账号和依据保留。'}
        else:values={'candidate_person_id':'','mapping_note':'用户否定此候选对应；账号保留，等待重新指定人员。'}
        return self.store.update('contacts',cid,values,revision,reason='人员档案账号对应确认',actor='用户')

    def relations(self,c,person_id=None,kind=None,target_id=None):
        if not self.exists(c):return []
        clauses=[];args=[]
        for key,value in [('person_id',person_id),('kind',kind),('target_id',target_id)]:
            if value is not None:clauses.append(key+'=?');args.append(value)
        query='SELECT * FROM wk_person_relations'+(' WHERE '+' AND '.join(clauses) if clauses else '')+' ORDER BY kind,target_id,label'
        rows=[]
        people=self.store._records(c,'people');targets={k:self.store._records(c,k) for k in ('projects','organizations','departments')}
        review_notes={r['review_key']:r['note'] for r in c.execute('SELECT * FROM wk_review_notes')} if c.execute("SELECT 1 FROM sqlite_master WHERE name='wk_review_notes'").fetchone() else {}
        for row in c.execute(query,args):
            d=self._decode(row)
            if d['person_id'] not in people:continue
            d.update(person_name=people[d['person_id']]['name'],target_name=targets[d['kind']].get(d['target_id'],{}).get('name','关联已不可用'),state_label=RELATION_STATES[d['state']])
            d['represented_org_name']=targets['organizations'].get(d.get('represented_org_id'),{}).get('name','')
            d['department_name']=targets['departments'].get(d.get('department_id'),{}).get('name','')
            d['review_note']=review_notes.get('role:'+d['id'],'')
            rows.append(d)
        return rows

    def index(self,c):
        people=self.store._records(c,'people');accounts=list(self.store._records(c,'contacts').values())
        relations=self.relations(c);orgs=self.store._records(c,'organizations');projects=self.store._records(c,'projects')
        out={}
        for pid,p in people.items():
            account=[a for a in accounts if not a.get('archived') and pid in (a.get('person_id'),a.get('candidate_person_id'))]
            rel=[r for r in relations if r['person_id']==pid and r['state']!='rejected']
            pending=sum(a.get('candidate_person_id')==pid for a in account)+sum(r['state']=='pending' for r in rel)+(p.get('state')=='待核对')
            pids=set(p.get('project_ids') or []) | {r['target_id'] for r in rel if r['kind']=='projects'}
            pids|={r['id'] for r in projects.values() if pid in (r.get('participant_ids') or []) or r.get('owner_id')==pid}
            orgnames=[orgs.get(p.get('organization_id'),{}).get('name','')]+[r['target_name'] for r in rel if r['kind']=='organizations']
            out[pid]={'accounts':len(account),'platforms':sorted({a.get('platform') for a in account if a.get('platform')}),
                'organization_ids':list(dict.fromkeys([p['organization_id']] if p.get('organization_id') else []))+[r['target_id'] for r in rel if r['kind']=='organizations' and r['state']!='pending'],
                'project_roles':[{k:r.get(k,'') for k in ('id','target_id','target_name','label','detail','state','phase','represented_org_name','department_name')} for r in rel if r['kind']=='projects'],
                'pending':pending,'missing_role':not bool(p.get('role')),'projects':sorted(pids),'organizations':list(dict.fromkeys(x for x in orgnames if x)),
                'departments':[dict(id=r['target_id'],name=r['target_name'],label=r['label'],state=r['state']) for r in rel if r['kind']=='departments'],
                'roles':[r['label'] for r in rel if r['kind']=='projects'],'review_reason':'、'.join(([f'{pending} 项待确认'] if pending else [])+(['职务待补'] if not p.get('role') else [])) or '已有关联资料'}
        return out

    def dossier(self,c,pid):
        p=self.store._record(c,'people',pid)
        accounts=[]
        for a in self.store._records(c,'contacts').values():
            if pid not in (a.get('person_id'),a.get('candidate_person_id')):continue
            account=dict(a)
            account['mapping_state']='candidate' if a.get('candidate_person_id')==pid else 'linked'
            account['sources']=a.get('_dossier_sources',[])
            if not account['sources']:
                account['sources']=[s for p in a.get('_provenance',{}).values() for s in p.get('sources',[])]
            if not account['sources']:
                account['sources']=[dict(type='message' if e.get('message_id') else 'document',id=e.get('message_id') or e.get('document_id'),quote=e['quote'],title=e.get('conversation_name') or e.get('title') or '原有工作依据') for e in self.store._evidence(c,a.get('evidence_ids',[]))]
            accounts.append(account)
        relations=self.relations(c,person_id=pid)
        linked_sources=[]
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='raw_sources'").fetchone():
            for row in c.execute("SELECT id,platform,kind,identity,name,state,config FROM raw_sources WHERE kind='chat'"):
                row=dict(row);projects=json.loads(row.pop('config')).get('projects',[])
                direct=[a for a in accounts if a.get('platform')==row['platform'] and a.get('account_id')==row['identity']]
                related=[r for r in relations if r['kind']=='projects' and r['state']!='rejected' and r['target_id'] in projects]
                if direct or related:
                    row['connection']='工作单聊（身份候选）' if direct and direct[0]['mapping_state']=='candidate' else '工作单聊' if direct else '相关项目来源（不代表已确认群成员）'
                    linked_sources.append(row)
        events=[]
        if self.exists(c):
            events=[dict(r) for r in c.execute('SELECT action,actor,happened_at FROM wk_person_events WHERE person_id=? ORDER BY id DESC LIMIT 15',(pid,))]
        # Account decisions use the shared business audit; include them even after
        # a rejected mapping removes that account from this person's current view.
        events += [dict(r) for r in c.execute('''SELECT action,actor,happened_at FROM wk_changes
          WHERE (kind='people' AND record_id=?) OR (kind='contacts' AND
          (json_extract(before_view,'$.person_id')=? OR json_extract(before_view,'$.candidate_person_id')=?
           OR json_extract(after_view,'$.person_id')=? OR json_extract(after_view,'$.candidate_person_id')=?))
          ORDER BY id DESC LIMIT 15''',(pid,pid,pid,pid,pid))]
        events=sorted(events,key=lambda e:e['happened_at'],reverse=True)[:15]
        return {'accounts':accounts,'relations':relations,'sources':linked_sources,'events':events,'summary':self.index(c)[pid]}

    def source(self,sid):
        with self.store.connect() as c:
            row=c.execute('SELECT * FROM wk_person_sources WHERE id=?',(sid,)).fetchone()
            if not row:raise StoreError('人员资料快照不存在。',404)
            return dict(row)
