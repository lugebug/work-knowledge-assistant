"""One review queue for identity clues, account mappings and project roles.

Decisions use the existing maintenance layer. Batches are all-or-nothing;
person merges preserve sources and can be undone as a whole before later edits.
"""
import hashlib
import json
import re
from model import KINDS, FIELDS, RELATIONS
from people import People, stable_id, RELATION_FIELDS, RELATION_EXTRAS
from store import StoreError, json_text, now
from completeness import Completeness


class PeopleReview:
    def __init__(self, store):
        self.store = store
        self.people = People(store)

    def initialize(self):
        self.people.initialize()
        Completeness(self.store).initialize()
        with self.store.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS wk_identity_reviews(
              id TEXT PRIMARY KEY,source_id TEXT NOT NULL,target_id TEXT NOT NULL,
              basis TEXT NOT NULL,sources TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
              revision INTEGER NOT NULL DEFAULT 1,note TEXT NOT NULL DEFAULT '',
              merge_backup TEXT NOT NULL DEFAULT '{}',updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wk_review_notes(
              review_key TEXT PRIMARY KEY,note TEXT NOT NULL,updated_at TEXT NOT NULL);
            ''')

    def seed_identity(self, source_id, target_id, basis, refs):
        """Called only for reviewed clues; never changes an existing decision."""
        with self.store.connect() as c:
            a=self.store._record(c,'people',source_id);b=self.store._record(c,'people',target_id)
            if source_id==target_id or a.get('archived') or b.get('archived'):
                raise StoreError('请选择两个未归档的不同档案。')
            sources=self.people._refs(c,refs)
            rid=stable_id('identity',source_id,target_id)
            c.execute('INSERT OR IGNORE INTO wk_identity_reviews(id,source_id,target_id,basis,sources,updated_at) VALUES(?,?,?,?,?,?)',
                      (rid,source_id,target_id,basis,json_text(sources),now()))
            return rid

    def _queue(self,c):
        people=self.store._records(c,'people');contacts=self.store._records(c,'contacts');orgs=self.store._records(c,'organizations')
        notes={r['review_key']:r['note'] for r in c.execute('SELECT * FROM wk_review_notes')}
        events={r['relation_id']:dict(r) for r in c.execute("SELECT * FROM wk_person_events WHERE action IN ('集中核对：confirm','集中核对：reject') ORDER BY id")}
        rows=[]
        for raw in c.execute('SELECT * FROM wk_identity_reviews ORDER BY id'):
            r=dict(raw);a=people[r['source_id']];b=people[r['target_id']]
            state=r['state']
            if state=='pending' and (a.get('archived') or b.get('archived')):
                state='superseded'
            description='可能是同一人；确认后将账号和已有关系集中到「'+b['name']+'」，原档案归档保留。'
            if state=='confirmed':description='已确认为同一人，账号和已有关系已集中到「'+b['name']+'」，原档案仍可查看。'
            elif state=='rejected':description='你已否定这条同人线索，两份档案保持独立。'
            rows.append(dict(key='identity:'+r['id'],kind='identity',id=r['id'],person_id=b['id'],
                source_id=a['id'],target_id=b['id'],title=a['name']+' ↔ '+b['name'],
                description=description,
                basis=r['basis'],sources=json.loads(r['sources']),state=state,note=r['note'],
                can_confirm=state=='pending',can_undo=state=='confirmed',
                source_name=a['name'],target_name=b['name']))
        for a in contacts.values():
            pid=a.get('candidate_person_id')
            last=events.get('account:'+a['id']);state='pending'
            if not pid and last:
                outcome=json.loads(last['after_json']).get('decision')
                if outcome in ('confirm','reject'):
                    state='confirmed' if outcome=='confirm' else 'rejected';pid=last['person_id']
            if not pid or a.get('archived') or people.get(pid,{}).get('archived'):continue
            rows.append(dict(key='account:'+a['id'],kind='account',id=a['id'],person_id=pid,
                title=a['name']+' → '+people[pid]['name'],description='确认这个聊天账号对应此人。',
                basis=a.get('mapping_note',''),sources=a.get('_dossier_sources',[]),state=state,
                note=notes.get('account:'+a['id'],a.get('notes','')),can_confirm=state=='pending'))
        for r in self.people.relations(c):
            if r['state']=='supported' or people[r['person_id']].get('archived'):continue
            rows.append(dict(key='role:'+r['id'],kind='role',id=r['id'],person_id=r['person_id'],
                title=r['person_name']+' · '+r['target_name'],description=r['label'],detail=r['detail'],
                target_id=r['target_id'],target_kind=r['kind'],basis=r['basis'],sources=r['sources'],
                state=r['state'],note=notes.get('role:'+r['id'],''),can_confirm=r['state']=='pending',
                **{key:r.get(key,'') for key in RELATION_EXTRAS}))
        for p in people.values():
            if p.get('archived') or p['state']!='待核对':continue
            accounts=[a for a in contacts.values() if a.get('person_id')==p['id'] and not a.get('archived')]
            refs=list(p.get('_dossier_sources',[]))
            for a in accounts:refs.extend(a.get('_dossier_sources',[]))
            rows.append(dict(key='profile:'+p['id'],kind='profile',id=p['id'],person_id=p['id'],
                title=p['name'],description='补充姓名、所属单位或工作角色；缺少的资料可以留空。',
                basis=(p.get('identity_basis','来自已有工作资料，具体身份尚待补充').replace('尚未对应平台账号','现已对应工作账号；单位与职务仍待补充') if accounts else p.get('identity_basis','来自已有工作资料，具体身份尚待补充')),sources=refs[:8],
                detail=' · '.join(x for x in (orgs.get(p.get('organization_id'),{}).get('name'),p.get('role'),p.get('work_scope')) if x),
                state='pending',note=p.get('notes',''),can_confirm=False,
                accounts=[dict(name=a['name'],platform=a.get('platform','')) for a in accounts]))
        # Covers existing maintenance entry points as well as this queue, without
        # timestamp precision races or trusting a browser-provided revision.
        completeness=Completeness(self.store).scan(c)
        rows.extend(completeness['items'])
        token_data=[rows,[tuple(r) for r in c.execute('SELECT kind,id,revision FROM wk_records ORDER BY kind,id')],
                    [tuple(r) for r in c.execute('SELECT id,revision FROM wk_person_relations ORDER BY id')]]
        version=hashlib.sha256(json_text(token_data).encode()).hexdigest()
        return dict(version=version,items=rows,completeness=completeness['summary'],critical_dimensions=completeness['rules'])

    def queue(self):
        with self.store.connect() as c:
            c.execute('BEGIN')
            from work_model import enabled
            if enabled(c):
                from judgment_cards import JudgmentCards
                cards = JudgmentCards(self.store)
                return dict(items=cards.queue_in_connection(c), summary=cards.summary(c))
            return self._queue(c)

    def _require_legacy(self):
        from work_model import enabled
        with self.store.connect() as c:
            if enabled(c):
                raise StoreError('旧版缺项表单已停用，请在具体问题卡中确认或修改。', 409)

    def complete(self,version,items):
        """Fill multiple records atomically with the same validation/audit as edits."""
        self._require_legacy()
        if not isinstance(items,list) or not 1<=len(items)<=100:
            raise StoreError('请选择1至100份档案。')
        if any(not isinstance(i,dict) or set(i)!={'key','answers'} for i in items):
            raise StoreError('补齐内容格式不正确。')
        keys=[i['key'] for i in items]
        if any(not isinstance(k,str) for k in keys) or len(set(keys))!=len(keys):
            raise StoreError('同一份档案不能重复提交。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current=self._queue(c)
            if current['version']!=version:
                raise StoreError('资料已被更新，本次未保存。请重新读取核对；已输入内容仍保留。',409)
            available={r['key']:r for r in current['items'] if r['kind']=='completeness'}
            results=[]
            for item in items:
                if item['key'] not in available:raise StoreError('档案已归档或缺项已变化，请重新读取。',409)
                results.append(Completeness(self.store).apply(c,available[item['key']],item['answers']))
            return dict(saved=len(results),dimensions=sum(r['answered'] for r in results),items=results)

    def _update(self,c,kind,rid,values,reason,action=None,actor='用户'):
        r=self.store._record(c,kind,rid)
        return self.store.update_in_transaction(c,kind,rid,values,r['revision'],actor,reason,action)

    def resolve_same_names(self):
        """Resolve reviewed, unique real-name pairs; preserve explicit decisions."""
        resolved=[]
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            people=self.store._records(c,'people')
            for raw in c.execute("SELECT * FROM wk_identity_reviews WHERE state='pending'").fetchall():
                r=dict(raw);a=people[r['source_id']];b=people[r['target_id']]
                name=a['name'].strip()
                if a.get('archived') or b.get('archived') or name!=b['name'].strip():continue
                if not re.fullmatch(r'[\u4e00-\u9fff·]{2,12}',name):continue
                if sum(not p.get('archived') and p['name'].strip()==name for p in people.values())!=2:continue
                if a.get('organization_id') and b.get('organization_id') and a['organization_id']!=b['organization_id']:continue
                if c.execute("SELECT 1 FROM wk_person_events WHERE relation_id=? AND action='撤销整次同人合并'",(r['id'],)).fetchone():continue
                reason='按用户规则直接对应：已审阅工作线索中姓名完全相同，无重名或单位冲突。'
                self._merge(c,r['id'],r['note'],actor='Codex',reason=reason)
                self.people._event(c,r['target_id'],'identity:'+r['id'],'同名直接对应',{'state':'pending'},{'state':'confirmed','reason':reason},'Codex')
                a['archived']=True
                resolved.append(dict(key='identity:'+r['id'],name=name))
        return dict(saved=len(resolved),items=resolved)

    def support_identity(self,rid,revision,refs,basis):
        """Apply a separately reviewed identity fact using the reversible merge."""
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            raw=c.execute('SELECT * FROM wk_identity_reviews WHERE id=?',(rid,)).fetchone()
            if not raw:raise StoreError('身份线索不存在。',404)
            r=dict(raw)
            if r['revision']!=revision:raise StoreError('身份线索已变化，请重新读取。',409)
            if r['state']!='pending' or c.execute("SELECT 1 FROM wk_person_events WHERE relation_id=? AND action='撤销整次同人合并'",(rid,)).fetchone():
                return dict(id=rid,protected=True)
            sources=self.people._refs(c,refs)
            if not isinstance(basis,str) or not basis.strip() or len(basis)>1000:raise StoreError('请说明身份依据。')
            c.execute('UPDATE wk_identity_reviews SET sources=?,basis=? WHERE id=?',(json_text(sources),basis,rid))
            self._merge(c,rid,r['note'],actor='Codex',reason=basis)
            self.people._event(c,r['target_id'],'identity:'+rid,'依据对应身份',{'state':'pending'},{'state':'confirmed','sources':sources,'basis':basis},'Codex依据整理')
            return dict(id=rid,saved=True)

    def decide(self,version,items):
        self._require_legacy()
        if not isinstance(items,list) or not 1<=len(items)<=100:
            raise StoreError('请选择1至100项处理。')
        if any(not isinstance(i,dict) or set(i)-{'key','decision','note','values'} for i in items):
            raise StoreError('核对内容格式不正确。')
        keys=[i.get('key') for i in items]
        if any(not isinstance(k,str) for k in keys) or len(keys)!=len(set(keys)):
            raise StoreError('同一项不能重复提交。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current=self._queue(c)
            if version!=current['version']:
                raise StoreError('核对清单已变化，本次没有保存。请重新读取后核对；输入备注仍保留。',409)
            available={r['key']:r for r in current['items']}
            results=[]
            # Merge last: any conflicting record in the batch causes rollback.
            items=sorted(items,key=lambda i:available.get(i['key'],{}).get('kind')=='identity')
            for request in items:
                r=available.get(request['key']);decision=request.get('decision')
                if not r or r['kind']=='completeness' or decision not in ('confirm','reject','note','undo'):
                    raise StoreError('核对项或处理方式无效。')
                if decision=='undo':
                    if r['kind']!='identity' or not r.get('can_undo'):raise StoreError('此项不能撤销合并。')
                    self._undo_merge(c,r['id']);results.append(r['key']);continue
                if r['state']!='pending' and not (r['state'] in ('confirmed','rejected') and (decision=='note' or r['kind']=='role')):
                    raise StoreError('此项已经处理；身份合并请先撤销，账号可在档案调整。',409)
                note=request.get('note',r['note'])
                if not isinstance(note,str) or len(note)>4000:raise StoreError('备注最多4000字。')
                note=note.strip();values=request.get('values',{})
                if not isinstance(values,dict):raise StoreError('修正内容格式不正确。')
                if values and (r['kind']!='role' or decision!='confirm'):raise StoreError('只有确认分工支持这些修正字段。')
                if r['kind']=='profile' and decision!='note':raise StoreError('身份资料需先补充，不能批量认定未知资料。')
                if r['kind']=='identity':
                    if decision=='confirm':self._merge(c,r['id'],note)
                    else:c.execute('UPDATE wk_identity_reviews SET state=?,note=?,revision=revision+1,updated_at=? WHERE id=?',
                        ('rejected' if decision=='reject' else r['state'],note,now(),r['id']))
                elif r['kind']=='account':
                    update={'notes':note}
                    if decision!='note':update.update(candidate_person_id='',mapping_note='用户在集中核对页确认此账号。' if decision=='confirm' else '用户在集中核对页否定此对应。')
                    if decision=='confirm':
                        a=self.store._record(c,'contacts',r['id'])
                        if a.get('person_id') and a['person_id']!=r['person_id']:raise StoreError('账号已有其他身份，请先调整。',409)
                        update.update(person_id=r['person_id'],state='工作联系人')
                    self._update(c,'contacts',r['id'],update,'集中核对账号：'+decision)
                elif r['kind']=='profile':self._update(c,'people',r['id'],{'notes':note},'集中核对补充备注')
                elif decision!='note':
                    raw=c.execute('SELECT * FROM wk_person_relations WHERE id=?',(r['id'],)).fetchone()
                    before=self.people._decode(raw);after=dict(before)
                    if set(values)-RELATION_FIELDS:raise StoreError('分工修正内容无效。')
                    after.update(values);self.people._validate_relation(c,after)
                    after.update(state='confirmed' if decision=='confirm' else 'rejected',revision=before['revision']+1,updated_at=now())
                    c.execute('UPDATE wk_person_relations SET label=?,detail=?,target_id=?,state=?,revision=?,updated_at=? WHERE id=?',
                        (after['label'],after['detail'],after['target_id'],after['state'],after['revision'],after['updated_at'],r['id']))
                    self.people._save_extras(c,r['id'],after)
                    self.people._event(c,r['person_id'],r['id'],'集中核对分工',before,dict(after,review_note=note),'用户')
                c.execute('INSERT INTO wk_review_notes VALUES(?,?,?) ON CONFLICT(review_key) DO UPDATE SET note=excluded.note,updated_at=excluded.updated_at',
                          (r['key'],note,now()))
                self.people._event(c,r['person_id'],r['key'],'集中核对：'+decision,{'state':r['state'],'note':r['note']},{'decision':decision,'note':note},'用户')
                results.append(r['key'])
            return dict(saved=len(results),keys=results)

    def _merge(self,c,rid,note,actor='用户',reason=None):
        review=dict(c.execute('SELECT * FROM wk_identity_reviews WHERE id=?',(rid,)).fetchone())
        source,target=review['source_id'],review['target_id']
        a=self.store._record(c,'people',source);b=self.store._record(c,'people',target)
        if a.get('archived') or b.get('archived'):raise StoreError('某个档案已归档，本批未保存；请重新读取。',409)
        changes=[];before_relations=[]
        reason=reason or '用户确认同人：'+a['name']+' → '+b['name']+'；原档案归档保留。'
        for kind in KINDS:
            for record in list(self.store._records(c,kind).values()):
                values={}
                for field,(destination,multiple) in RELATIONS.items():
                    if destination!='people' or field not in FIELDS[kind]:continue
                    value=record.get(field,[] if multiple else '')
                    if multiple and source in (value or []):values[field]=list(dict.fromkeys(target if x==source else x for x in value))
                    elif not multiple and value==source:values[field]=target
                if kind=='contacts':
                    actual=values.get('person_id',record.get('person_id'))
                    candidate=values.get('candidate_person_id',record.get('candidate_person_id'))
                    if candidate and candidate==actual:values['candidate_person_id']=''
                if kind=='people' and record['id']==target:
                    values['project_ids']=list(dict.fromkeys([*b.get('project_ids',[]),*a.get('project_ids',[])]))
                if kind=='people' and record['id']==source:
                    values.update(archived=True,notes=(a.get('notes','')+'\n已确认与「'+b['name']+'」同人；主档案：'+target).strip())
                if values:
                    result=self._update(c,kind,record['id'],values,reason,'合并人员',actor)
                    if not result.get('unchanged'):changes.append(result['change_id'])
        for raw in c.execute('SELECT * FROM wk_person_relations WHERE person_id=?',(source,)).fetchall():
            before=self.people._decode(raw);before_relations.append(before)
            c.execute('UPDATE wk_person_relations SET person_id=?,revision=revision+1,updated_at=? WHERE id=?',(target,now(),before['id']))
            self.people._event(c,target,before['id'],'合并人员关系',before,dict(before,person_id=target,revision=before['revision']+1),actor)
        backup=dict(changes=changes,relations=before_relations)
        c.execute("UPDATE wk_identity_reviews SET state='confirmed',note=?,merge_backup=?,revision=revision+1,updated_at=? WHERE id=?",
                  (note,json_text(backup),now(),rid))

    def _undo_merge(self,c,rid):
        review=dict(c.execute('SELECT * FROM wk_identity_reviews WHERE id=?',(rid,)).fetchone())
        backup=json.loads(review['merge_backup']);changes=[]
        for cid in backup['changes']:
            ch=dict(c.execute('SELECT * FROM wk_changes WHERE id=?',(cid,)).fetchone())
            current=c.execute('SELECT * FROM wk_records WHERE kind=? AND id=?',(ch['kind'],ch['record_id'])).fetchone()
            if not current or current['revision']!=ch['after_revision'] or current['payload']!=ch['after_payload']:
                raise StoreError('合并后已有后续维护，不能直接撤销覆盖；请先检查当前档案。',409)
            changes.append((ch,dict(current)))
        for r in backup['relations']:
            current=c.execute('SELECT * FROM wk_person_relations WHERE id=?',(r['id'],)).fetchone()
            if not current or current['revision']!=r['revision']+1 or current['person_id']!=review['target_id']:
                raise StoreError('合并后关系已变化，不能直接撤销。',409)
        for ch,current in reversed(changes):
            self.store._write(c,ch['kind'],ch['record_id'],json.loads(ch['before_payload']),json.loads(ch['after_payload']),
                current['revision'],'撤销人员合并','用户','整次撤销同人合并',bool(current['created']))
        for r in backup['relations']:
            c.execute('UPDATE wk_person_relations SET person_id=?,revision=revision+1,updated_at=? WHERE id=?',(r['person_id'],now(),r['id']))
            self.people._event(c,r['person_id'],r['id'],'撤销人员关系合并',{'person_id':review['target_id']},{'person_id':r['person_id']},'用户')
        c.execute("UPDATE wk_identity_reviews SET state='pending',merge_backup='{}',revision=revision+1,updated_at=? WHERE id=?",(now(),rid))
        self.people._event(c,review['target_id'],rid,'撤销整次同人合并',{'state':'confirmed'},{'state':'pending'},'用户')
