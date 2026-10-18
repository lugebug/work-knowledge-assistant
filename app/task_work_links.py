"""Stable, reviewed Feishu-task to local-work associations, with explicit decisions."""
from contextlib import nullcontext
import hashlib
import json

from store import StoreError, json_text, now


class TaskWorkLinks:
    def __init__(self, store):
        self.store = store

    def initialize(self):
        with self.store.connect() as c:
            c.executescript('''
              CREATE TABLE IF NOT EXISTS wk_task_work_links(
                source_id TEXT PRIMARY KEY, payload TEXT NOT NULL, revision INTEGER NOT NULL,
                updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS wk_task_link_events(
                id INTEGER PRIMARY KEY, source_id TEXT NOT NULL, before_json TEXT NOT NULL,
                after_json TEXT NOT NULL, actor TEXT NOT NULL, happened_at TEXT NOT NULL);
            ''')

    @staticmethod
    def exists(c):
        return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='wk_task_work_links'").fetchone())

    def get(self, c, source_id):
        if not self.exists(c):
            return None
        row = c.execute('SELECT * FROM wk_task_work_links WHERE source_id=?', (source_id,)).fetchone()
        return dict(json.loads(row['payload']), source_id=source_id, revision=row['revision'], updated_at=row['updated_at']) if row else None

    def _source(self, c, version_id):
        row = c.execute('''SELECT s.*,v.payload AS raw_payload,h.version_id AS current_version FROM raw_versions v
            JOIN raw_sources s ON s.id=v.source_id LEFT JOIN raw_heads h
            ON h.source_id=v.source_id AND h.object_id=v.object_id WHERE v.id=?''', (version_id,)).fetchone()
        if not row or row['platform'] != 'feishu' or row['kind'] != 'task' or row['state'] != 'approved':
            raise StoreError('需要当前获准的飞书任务原文版本。', 403)
        if row['current_version'] != version_id:
            raise StoreError('飞书任务已有新版本，请先核对最新范围并更新关联判断。', 409)
        return dict(row)

    def validate_links(self, c, links):
        if not isinstance(links, list) or len(links) > 20:
            raise StoreError('任务关联应为最多20项工作；无关联须说明原因。')
        seen, result = set(), []
        for link in links:
            if not isinstance(link, dict) or set(link) != {'work_id','relation','reason'}:
                raise StoreError('关联需说明原工作、同一事项或其中一部分，以及依据。')
            if link['relation'] not in ('same','part') or not isinstance(link['reason'],str) or not 8 <= len(link['reason'].strip()) <= 2000:
                raise StoreError('请说明同一事项或子项关联的具体依据。')
            if link['work_id'] in seen:
                raise StoreError('同一工作不能重复关联。')
            self.store._record(c,'tasks',link['work_id'])
            seen.add(link['work_id']); result.append(dict(link))
        if len(result) > 1 and any(x['relation']=='same' for x in result):
            raise StoreError('同一飞书任务不能同时与多项工作完全等同；确为拆分工作时请注明子项关联。')
        return sorted(result, key=lambda x:x['work_id'])

    @staticmethod
    def identity(links):
        return sorted((x['work_id'],x['relation']) for x in links)

    def _write(self, c, source_id, payload, actor):
        old = c.execute('SELECT * FROM wk_task_work_links WHERE source_id=?', (source_id,)).fetchone()
        revision = old['revision'] + 1 if old else 1
        c.execute('INSERT INTO wk_task_work_links VALUES(?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision,updated_at=excluded.updated_at',
            (source_id,json_text(payload),revision,now()))
        after = self.get(c,source_id)
        event = c.execute('INSERT INTO wk_task_link_events(source_id,before_json,after_json,actor,happened_at) VALUES(?,?,?,?,?)',
            (source_id,json_text(dict(old)) if old else '{}',json_text(after),actor,now()))
        return dict(after, link_event_id=event.lastrowid)

    def bind(self, document, actor='Codex依据整理', *, connection=None):
        if not isinstance(document,dict) or set(document) != {'version_id','revision','links','reason'} or type(document['revision']) is not int:
            raise StoreError('关联需提供任务证据版本、关联版本、工作及判断理由。')
        if not isinstance(document['reason'],str) or not 8 <= len(document['reason'].strip()) <= 3000:
            raise StoreError('请说明任务为何归入这些工作，或为何不归入。')
        with (nullcontext(connection) if connection is not None else self.store.connect()) as c:
            if connection is None: c.execute('BEGIN IMMEDIATE')
            source = self._source(c,document['version_id']); sid=source['id']
            old = self.get(c,sid)
            if document['revision'] != (old['revision'] if old else 0):
                raise StoreError('任务关联已变化，请重新读取。',409)
            links = self.validate_links(c,document['links'])
            if old and old['status'] != 'pending':
                if self.identity(old['links']) == self.identity(links):
                    return dict(old,unchanged=True)
                if actor != '用户':
                    raise StoreError('已有任务关联或用户不归入的决定受保护；不能另建或静默换工作。',409)
            if old and old.get('judgment_id') and actor != '用户':
                question = c.execute('SELECT state FROM wk_judgment_cards WHERE id=?',(old['judgment_id'],)).fetchone()
                if question and question[0] in ('resolved','dismissed'):
                    raise StoreError('该任务关联已有用户决定，请按原决定处理。',409)
            result = self._write(c,sid,dict(status='linked' if links else 'ignored',links=links,
                version_id=document['version_id'],reason=document['reason'],mode='user' if actor=='用户' else 'fact',
                judgment_id=old.get('judgment_id','') if old else ''),actor)
            if old and old['status']=='pending' and actor != '用户':
                from work_lifecycle import WorkLifecycle
                WorkLifecycle(self.store)._resolve_question(c,old['judgment_id'],document['version_id'])
            return result

    def propose(self, document):
        required={'version_id','revision','question','background','reason','unknowns','candidates'}
        if not isinstance(document,dict) or set(document)!=required:
            raise StoreError('关联疑点须提供背景、依据、候选工作及当前关联版本。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            source=self._source(c,document['version_id']); sid=source['id']; old=self.get(c,sid)
            if document['revision'] != (old['revision'] if old else 0):
                raise StoreError('任务关联已变化，请重新核对。',409)
            if old and old['status']!='pending':
                raise StoreError('已有明确任务关联，不重开新问题。',409)
            candidates=document['candidates']
            if not isinstance(candidates,list) or not 1 <= len(candidates) <= 3:
                raise StoreError('请给出1至3项已经核对的候选工作。')
            candidates=[self.validate_links(c,[x])[0] for x in candidates]
            qid='task-link-'+hashlib.sha256(sid.encode()).hexdigest()[:24]
            payload=json.loads(source['raw_payload'])
            options=[]; mappings={}
            for index,link in enumerate(candidates):
                option_id='work-'+str(index)
                work=self.store._record(c,'tasks',link['work_id'])
                options.append(dict(id=option_id,label='归入「'+work['title']+'」',description=link['reason'],operations=[]))
                mappings[option_id]=dict(version_id=document['version_id'],links=[link],reason=link['reason'])
            options.append(dict(id='ignore',label='暂不归入现有工作',description='保留任务来源及本次决定，不另建重复工作。',operations=[]))
            mappings['ignore']=dict(version_id=document['version_id'],links=[],reason='用户确认本次任务不归入所列现有工作，保留来源，不自动新建。')
            from judgment_cards import JudgmentCards
            prior=c.execute('SELECT payload FROM wk_judgment_cards WHERE id=?',(qid,)).fetchone()
            receipts=list(dict.fromkeys([*(json.loads(prior[0]).get('receipt_ids',[]) if prior else []),document['version_id']]))
            card=dict(id=qid,category='ambiguity',question=document['question'],background=document['background'],
                impact='确认后保存飞书任务与原工作的长期关联；以后变化优先回到该工作，不重复新建。',
                recommendation='请核对以下任务范围与候选工作，选择实际对应关系。',rationale=document['reason'],
                unknowns=document['unknowns'],checked='已核对飞书任务及所列现有工作；尚不能直接确定同一事项。',
                sources=[dict(type='raw',id=document['version_id'],quote='任务：'+payload['title'])],
                receipt_ids=receipts,options=options,recommended_option=options[0]['id'],task_link_options=mappings)
            saved=JudgmentCards(self.store).propose(card,connection=c)
            if saved.get('protected'):
                return dict(old or {},protected=True,judgment_id=qid)
            return self._write(c,sid,dict(status='pending',links=[],candidates=candidates,judgment_id=qid,
                version_id=document['version_id'],reason=document['reason'],mode='fact'),'Codex依据整理')

    def require_review(self, c, version_id, related, sources):
        row=c.execute('SELECT source_id,object_kind FROM raw_versions WHERE id=?',(version_id,)).fetchone()
        if not row or row['object_kind']!='task': return []
        link=self.get(c,row['source_id'])
        if not link:
            raise StoreError('飞书任务须先核对并保存现有工作关联、明确新建或关联疑点；不能跳过防重复检查。',409)
        if any(not isinstance(x,dict) or 'work_id' not in x or 'action' not in x for x in related):
            raise StoreError('每项任务变化需明确关联工作及处理方式。')
        by_work={x['work_id']:x for x in related}
        for binding in link['links']:
            if binding['work_id'] not in by_work:
                raise StoreError('本条任务变化须逐项核对已关联的全部工作，不能遗漏原工作。',409)
            if binding['relation']=='part' and by_work[binding['work_id']]['action']=='close' and not any(r['id']!=version_id for r in sources):
                raise StoreError('任务只对应原工作的一部分；关闭整项工作需要补充其他完成依据。',409)
        if link['status']=='pending':
            from judgment_cards import JudgmentCards
            row=c.execute('SELECT payload,state FROM wk_judgment_cards WHERE id=?',(link['judgment_id'],)).fetchone()
            if row and row['state'] in ('pending','deferred'):
                card=json.loads(row['payload'])
                if version_id not in card['receipt_ids']:
                    card['receipt_ids'].append(version_id)
                    JudgmentCards(self.store).propose(card,connection=c)
                return [link['judgment_id']]
        return []

    def for_work(self, c, work_id):
        if not self.exists(c): return []
        result=[]
        for row in c.execute('SELECT source_id FROM wk_task_work_links ORDER BY source_id'):
            link=self.get(c,row['source_id'])
            matching=next((x for x in link['links'] if x['work_id']==work_id),None)
            if not matching: continue
            source=c.execute('SELECT name,state,config FROM raw_sources WHERE id=?',(row['source_id'],)).fetchone()
            head=c.execute('SELECT v.payload FROM raw_heads h JOIN raw_versions v ON v.id=h.version_id WHERE h.source_id=?',(row['source_id'],)).fetchone()
            task=json.loads(head[0]).get('task',{}) if head else {}
            result.append(dict(source_id=row['source_id'],name=task.get('summary') or source['name'],source_state=source['state'],
                url=json.loads(source['config']).get('url',''),relation=matching['relation'],reason=matching['reason'],
                status=task.get('status',''),due=task.get('due') or {},updated_at=task.get('updated_at','')))
        return result

    def stats(self,c):
        if not self.exists(c): return {}
        records=[self.get(c,r[0]) for r in c.execute('SELECT source_id FROM wk_task_work_links')]
        total=c.execute("SELECT count(*) FROM raw_sources WHERE platform='feishu' AND kind='task' AND state='approved'").fetchone()[0]
        return dict(total_sources=total,linked=sum(x['status']=='linked' for x in records),
            pending=sum(x['status']=='pending' for x in records),ignored=sum(x['status']=='ignored' for x in records),
            unassessed=c.execute("SELECT count(*) FROM raw_sources s LEFT JOIN wk_task_work_links l ON l.source_id=s.id WHERE s.platform='feishu' AND s.kind='task' AND s.state='approved' AND l.source_id IS NULL").fetchone()[0])
