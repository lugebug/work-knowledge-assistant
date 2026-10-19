"""Evidence-aware follow-through checks, performed by Codex rather than the user.

This ledger does not infer a task or a question from a blank field. A reviewer
must account for actions and consequential uncertainty against a current
snapshot. Changed business context, user decisions and linked outcomes make the
review stale. Historical source receipts stay immutable.
"""
from copy import deepcopy
import hashlib
import json

from model import FIELDS
from store import StoreError, json_text, now
from work_analysis import source_refs

KINDS = ('projects', 'tasks')
FACETS = {'completion': '完成判断', 'waiting': '等待条件', 'scope': '阶段范围',
          'parties': '组织分工', 'timing': '时间安排'}
INSTRUCTIONS = (
    '这是Codex整理检查，不是用户必填表单。先核对现有资料、用户决定和关联工作。'
    '项目的下一步、已确认决定及已完成工作的独立后续，逐项关联已有工作；明确独立安排才新建工作，'
    '分析建议使用工作建议入口，不改成已承接责任。没有独立动作时说明原因。'
    '只有会影响当前推进、结果判断或时间安排且无法从资料查明的问题才进入待你判断；'
    '记录中的待明确、待核实不能只停在分析文字，须说明已补事实、已有问题卡或暂不影响行动。'
    '普通空白不列问题，同一事实不重复询问；已处理决定和人工维护受保护。'
    '完成判断、等待条件、阶段范围、组织分工、时间只在有用时补充，可留空；'
    '事实与分析分开，责任仍归实际承接人。保存检查不会替用户确认问题或改变业务状态。'
)


def digest(value):
    return hashlib.sha256(json_text(value).encode('utf-8')).hexdigest()


def text(value, label, minimum=1, maximum=6000):
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise StoreError('请说明' + label + '。')
    return value.strip()


class FollowThrough:
    def __init__(self, store):
        self.store = store

    def initialize(self):
        with self.store.connect() as c:
            c.executescript('''
              CREATE TABLE IF NOT EXISTS wk_followthrough_reviews(
                kind TEXT NOT NULL,record_id TEXT NOT NULL,input_token TEXT NOT NULL,
                payload TEXT NOT NULL,revision INTEGER NOT NULL,reviewed_at TEXT NOT NULL,
                PRIMARY KEY(kind,record_id));
              CREATE TABLE IF NOT EXISTS wk_followthrough_events(
                id INTEGER PRIMARY KEY,kind TEXT NOT NULL,record_id TEXT NOT NULL,
                before_json TEXT NOT NULL,after_json TEXT NOT NULL,actor TEXT NOT NULL,happened_at TEXT NOT NULL);
            ''')

    def enable(self):
        self.initialize()
        with self.store.connect() as c:
            c.execute("INSERT OR REPLACE INTO wk_meta VALUES('followthrough_version','37')")
        return {'enabled': True, 'version': 37}

    @staticmethod
    def enabled(c):
        row = c.execute("SELECT value FROM wk_meta WHERE key='followthrough_version'").fetchone()
        return bool(row and json.loads(row[0]) >= 37)

    @staticmethod
    def exists(c, name='wk_followthrough_reviews'):
        return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name=?", (name,)).fetchone())

    def context(self, c):
        from incremental import enabled, analysis_content, provenance
        incremental = enabled(c)
        records = {kind: self.store._records(c, kind) for kind in KINDS}
        cards, by_target = {}, {}
        if self.exists(c, 'wk_judgment_cards'):
            for row in c.execute('SELECT * FROM wk_judgment_cards ORDER BY id'):
                card = json.loads(row['payload'])
                card.update(state=row['state'], answer=row['answer'], revision=row['revision'])
                cards[row['id']] = card
                targets = {(op['kind'], op['id']) for opt in card['options'] for op in opt['operations']}
                for mapping in (card.get('task_link_options') or {}).values():
                    targets.update(('tasks', link['work_id']) for link in mapping['links'])
                for target in targets:
                    by_target.setdefault(target, []).append(row['id'])
        evidence = {}
        if self.exists(c, 'wk_work_evidence_reviews'):
            for row in c.execute('SELECT version_id,result FROM wk_work_evidence_reviews ORDER BY version_id'):
                for item in json.loads(row['result'])['outcomes']:
                    evidence.setdefault(item['work_id'], []).append((row['version_id'], item))
        raw = {}
        if self.exists(c, 'raw_heads'):
            raw = {r['id']: (r['state'], r['revision'], r['version_id']) for r in c.execute('''
              SELECT v.id,s.state,s.revision,h.version_id FROM raw_versions v
              JOIN raw_sources s ON s.id=v.source_id
              LEFT JOIN raw_heads h ON h.source_id=v.source_id AND h.object_id=v.object_id''')}
            if incremental and __import__('incremental').source_delta(c):
                raw = {vid: state[:2] for vid, state in raw.items()}
        reviews = {}
        if self.exists(c):
            reviews = {(r['kind'], r['record_id']): dict(r) for r in c.execute('SELECT * FROM wk_followthrough_reviews')}
        proposals = {}
        if self.exists(c, 'wk_work_proposals'):
            proposals = {r['id']: dict(r) for r in c.execute('SELECT * FROM wk_work_proposals')}
        hashes = {}
        for kind in KINDS:
            for rid, record in records[kind].items():
                value = {k: record.get(k) for k in FIELDS[kind] if kind != 'projects' or k != 'priority'}
                value['provenance'] = {k:v for k,v in record.get('_provenance', {}).items() if kind != 'projects' or k != 'priority'}
                # Analysis is part of the semantic context, but saving this
                # separate review never changes the record or its analysis.
                value['analysis'] = record.get('_work_analysis') or {}
                if kind == 'tasks':
                    from work_completion import WorkCompletion
                    goal = WorkCompletion.goal(c, rid)
                    if goal:
                        value['completion_goal'] = goal
                value['decisions'] = record.get('_work_analysis_decisions') or {}
                value['cards'] = [cards[qid] for qid in by_target.get((kind, rid), [])
                    if not (kind == 'projects' and qid.startswith('project-priority-v43-') and
                        all(op['kind'] == 'projects' and set(op['values']) == {'priority'}
                            for option in cards[qid]['options'] for op in option['operations']))]
                value['card_sources'] = [(ref['id'],raw.get(ref['id']))
                    for card in value['cards'] for ref in card.get('sources',[]) if ref.get('type')=='raw']
                value['evidence'] = evidence.get(rid, []) if kind == 'tasks' else []
                source_record = dict(record, _provenance=value['provenance'])
                value['sources'] = sorted((r['id'], raw.get(r['id'])) for r in source_refs(source_record) if r.get('type') == 'raw')
                if incremental:
                    value['analysis'] = analysis_content(value['analysis'])
                    value['provenance'] = provenance(value['provenance'])
                    # Evidence receipts prove processing, not a change of business facts.
                    value.pop('evidence', None)
                    value.pop('priority', None)
                    value['provenance'].pop('priority', None)
                hashes[(kind, rid)] = digest(value)
        impacts = {(r['kind'], r['record_id']): r['generation'] for r in c.execute(
            'SELECT * FROM wk_incremental_priority_impacts')} if incremental else {}
        return dict(records=records, cards=cards, by_target=by_target, hashes=hashes,
                    reviews=reviews, proposals=proposals, raw=raw, incremental=incremental, priority_impacts=impacts)

    def token(self, env, kind, rid):
        record = env['records'].get(kind, {}).get(rid)
        if not record or record.get('archived'):
            raise StoreError('检查对象不存在或已归档。', 404)
        related = []
        if kind == 'projects':
            related = sorted((wid, env['hashes'][('tasks', wid)]) for wid, w in env['records']['tasks'].items()
                             if w.get('project_id') == rid)
        elif not env.get('incremental') and record.get('project_id') in env['records']['projects']:
            pid = record['project_id']
            related = [(pid, env['hashes'][('projects', pid)])]
        # Outcomes referenced by a review can change without changing the source
        # record (e.g. an independently linked task or a proposed action).
        prior = env['reviews'].get((kind, rid))
        linked = []
        if prior:
            payload = json.loads(prior['payload'])
            for action in payload['actions']:
                for wid in action.get('work_ids', []):
                    linked.append(('work', wid, env['hashes'].get(('tasks', wid))))
                for qid in action.get('judgment_ids', []):
                    linked.append(('question', qid, env['cards'].get(qid)))
                for pid in action.get('proposal_ids', []):
                    linked.append(('proposal', pid, env['proposals'].get(pid)))
            for issue in payload['uncertainties']:
                qid = issue.get('judgment_id')
                if qid:
                    linked.append(('question', qid, env['cards'].get(qid)))
            for entry in [*payload.get('facets',{}).values(), *payload['uncertainties']]:
                for ref in entry.get('sources',[]):
                    if ref.get('type')=='raw':
                        linked.append(('source',ref['id'],env['raw'].get(ref['id'])))
        return digest({'self': env['hashes'][(kind, rid)], 'related': related, 'linked': linked})

    def rows(self, c, env=None):
        result = []
        if not self.enabled(c):
            return result
        env = env or self.context(c)
        for kind in KINDS:
            for rid, record in env['records'][kind].items():
                if record.get('archived'):
                    continue
                prior = env['reviews'].get((kind, rid))
                token = self.token(env, kind, rid)
                needed = not prior or prior['input_token'] != token
                result.append(dict(kind=kind, id=rid, name=record.get('name') or record.get('title'),
                    needs_review=needed, reason='工作、项目、依据或决定有变化' if prior else '尚未核对动作承接和关键未知',
                    context_token=token, reviewed_at=prior['reviewed_at'] if prior else ''))
        return result

    def status(self, c=None, env=None):
        if c is None:
            with self.store.connect() as conn:
                conn.execute('BEGIN')
                return self.status(conn)
        rows = self.rows(c, env)
        pending = [r for r in rows if r['needs_review']]
        return dict(enabled=self.enabled(c), total=len(rows), checked=len(rows)-len(pending),
                    pending=len(pending), pending_projects=sum(r['kind']=='projects' for r in pending),
                    pending_work=sum(r['kind']=='tasks' for r in pending), items=pending,
                    description='这些是Codex的整理核对；只有影响当前工作且无法查明的具体问题才交给你。')

    def packet(self, limit=20, targets=None):
        if type(limit) is not int or not 1 <= limit <= 250:
            raise StoreError('每批核对1至250项。')
        with self.store.connect() as c:
            c.execute('BEGIN')
            env = self.context(c)
            rows = self.rows(c, env)
            wanted = {(t['kind'], t['id']) for t in targets} if targets else None
            if wanted and wanted - {(r['kind'],r['id']) for r in rows}:
                raise StoreError('检查对象不在当前有效档案内。', 404)
            items = []
            for row in rows:
                key = row['kind'], row['id']
                if (wanted is not None and key not in wanted) or (wanted is None and not row['needs_review']):
                    continue
                record = env['records'][row['kind']][row['id']]
                project_id = row['id'] if row['kind']=='projects' else record.get('project_id')
                works = [r for r in env['records']['tasks'].values() if r.get('project_id') == project_id] if project_id else []
                prior = env['reviews'].get(key)
                items.append(dict(row, record=record, related_work=works,
                    decisions=[env['cards'][qid] for qid in env['by_target'].get(key,[])],
                    previous=json.loads(prior['payload']) if prior else None, sources=source_refs(record)))
                if len(items) == limit:
                    break
            return dict(items=items, pending=sum(r['needs_review'] for r in rows), instructions=INSTRUCTIONS)

    @staticmethod
    def _question(env, qid, kind, rid, pending=True):
        card = env['cards'].get(qid)
        if not card or (pending and card['state'] not in ('pending', 'deferred')):
            raise StoreError('关键未知需关联仍有效的可见问题；已处理决定不得当成未解决问题重开。')
        if qid not in env['by_target'].get((kind,rid),[]):
            # A meeting date or phase boundary can affect several works. Reuse
            # one actual question within the same project rather than asking
            # the user to answer the same fact separately for each work.
            record=env['records'].get(kind,{}).get(rid,{})
            pid=rid if kind=='projects' else record.get('project_id')
            shared=bool(pid) and any(
                qid in ids and ((target[0]=='projects' and target[1]==pid) or
                (target[0]=='tasks' and env['records']['tasks'].get(target[1],{}).get('project_id')==pid))
                for target,ids in env['by_target'].items())
            if not shared:
                raise StoreError('问题卡必须实际关联本次检查的项目或工作。')
        for opt in card['options']:
            for op in opt['operations']:
                target = env['records'].get(op['kind'],{}).get(op['id'])
                if op['kind'] in KINDS and (not target or target.get('archived')):
                    raise StoreError('问题关联记录不可用，请先核对问题。')
        return card

    def _validate(self, c, env, item):
        required = {'kind','id','context_token','checked','actions','no_action_reason','uncertainties','uncertainty_summary'}
        if not isinstance(item,dict) or required-set(item) or set(item)-required-{'facets'}:
            raise StoreError('请同时核对动作承接、关键未知及当前上下文。')
        kind, rid = item['kind'], item['id']
        if kind not in KINDS or self.token(env,kind,rid) != item['context_token']:
            raise StoreError('项目、工作、依据或决定已变化，请读取最新内容后重新核对。',409)
        clean = deepcopy(item)
        clean['checked'] = text(item['checked'],'已查范围和业务判断',8)
        clean['uncertainty_summary'] = text(item['uncertainty_summary'],'关键未知的核对结论',8)
        for name in ('actions','uncertainties'):
            if not isinstance(item[name],list) or len(item[name])>40:
                raise StoreError('动作和关键未知应逐项列出，每类不超过40项。')
            keys=[x.get('key') for x in item[name] if isinstance(x,dict)]
            if len(keys)!=len(item[name]) or any(not isinstance(k,str) or not k or len(k)>160 for k in keys) or len(set(keys))!=len(keys):
                raise StoreError('同一事项使用稳定且不重复的标识。')
        if not item['actions']:
            clean['no_action_reason']=text(item['no_action_reason'],'没有独立后续动作的具体原因',8)
        elif not isinstance(item['no_action_reason'],str):
            raise StoreError('没有独立动作的说明格式不正确。')
        for action in clean['actions']:
            if set(action)-{'key','text','disposition','reason','work_ids','proposal_ids','judgment_ids'}:
                raise StoreError('动作承接格式不正确。')
            for field in ('text','reason'):
                action[field]=text(action.get(field),'动作内容及承接依据',4)
            disposition=action.get('disposition')
            names={'linked':'work_ids','proposal':'proposal_ids','question':'judgment_ids','no_action':None}
            if disposition not in names:
                raise StoreError('动作需关联工作、工作建议、必要问题或说明无需独立推进。')
            for field in ('work_ids','proposal_ids','judgment_ids'):
                ids=action.get(field,[])
                if not isinstance(ids,list) or any(not isinstance(v,str) for v in ids) or len(set(ids))!=len(ids):
                    raise StoreError('动作承接对象格式不正确。')
                if names[disposition]==field and not ids:
                    raise StoreError('不能只写行动描述，须关联实际承接的记录。')
                if names[disposition]!=field and ids:
                    raise StoreError('动作承接类型与实际关联不一致。')
            for wid in action.get('work_ids',[]):
                work=env['records']['tasks'].get(wid)
                if not work or work.get('archived'):
                    raise StoreError('承接工作不存在或已归档。')
            for pid in action.get('proposal_ids',[]):
                if pid not in env['proposals']:
                    raise StoreError('工作建议尚未保存。')
            for qid in action.get('judgment_ids',[]):
                self._question(env,qid,kind,rid)
        from people import People
        for issue in clean['uncertainties']:
            if set(issue)-{'key','text','impact','disposition','reason','judgment_id','sources'}:
                raise StoreError('关键未知格式不正确。')
            for field in ('text','impact','reason'):
                issue[field]=text(issue.get(field),'具体未知、实际影响及处理依据',4)
            mode=issue.get('disposition')
            if mode=='question':
                self._question(env,issue.get('judgment_id'),kind,rid)
            elif mode=='resolved':
                qid=issue.get('judgment_id')
                if qid:
                    card=self._question(env,qid,kind,rid,pending=False)
                    if card['state'] not in ('resolved','dismissed'):
                        raise StoreError('问题仍未解决，不能登记为已经明确。')
                else:
                    issue['sources']=People._refs(c,issue.get('sources'))
            elif mode=='not_needed':
                if issue.get('judgment_id'):
                    raise StoreError('仍有关联问题时不能直接登记为无需判断；请先处理问题。')
            else:
                raise StoreError('关键未知须说明已有问题、已查明或暂不影响当前工作。')
        # A declared question cannot silently disappear from the next review.
        existing = env['reviews'].get((kind,rid))
        if existing:
            old=json.loads(existing['payload'])
            old_ids={q for a in old['actions'] for q in a.get('judgment_ids',[])} | {u.get('judgment_id') for u in old['uncertainties'] if u.get('judgment_id')}
            new_ids={q for a in clean['actions'] for q in a.get('judgment_ids',[])} | {u.get('judgment_id') for u in clean['uncertainties'] if u.get('judgment_id')}
            if any(env['cards'].get(q,{}).get('state') in ('pending','deferred') for q in old_ids-new_ids):
                raise StoreError('先前登记的关键问题仍未解决，本次检查不能漏掉它。')
        # Every current material question on this record must remain visible in
        # the assessment, including questions created by another entry point.
        declared={q for a in clean['actions'] for q in a.get('judgment_ids',[])} | {u.get('judgment_id') for u in clean['uncertainties'] if u.get('judgment_id')}
        outstanding={q for q in env['by_target'].get((kind,rid),[]) if env['cards'][q]['state'] in ('pending','deferred')}
        if outstanding-declared:
            raise StoreError('这项记录还有待你判断，请在关键未知中列出全部现有问题。')
        facets=clean.setdefault('facets',{})
        if not isinstance(facets,dict) or set(facets)-set(FACETS):
            raise StoreError('补充维度仅用于当前事项需要的完成、等待、范围、组织或时间信息。')
        for key, facet in facets.items():
            if not isinstance(facet,dict) or set(facet)-{'text','mode','sources'} or facet.get('mode') not in ('fact','analysis'):
                raise StoreError('补充维度须区分事实与分析；不要求填写无关信息。')
            facet['text']=text(facet.get('text'),'有助当前推进的补充信息')
            if facet['mode']=='fact' or facet.get('sources'):
                facet['sources']=People._refs(c,facet.get('sources'))
        return clean

    def review(self, document, actor='Codex工作承接核对'):
        items=document.get('items') if isinstance(document,dict) and set(document)=={'items'} else None
        if not isinstance(items,list) or not 1<=len(items)<=250:
            raise StoreError('请提交1至250项逐项检查结果。')
        results=[]
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not self.enabled(c):
                raise StoreError('工作承接检查尚未启用。')
            env=self.context(c)
            seen=set()
            for item in items:
                key=item.get('kind'),item.get('id')
                if key in seen:
                    raise StoreError('不能在同一批次重复检查同一记录。')
                seen.add(key)
                clean=self._validate(c,env,item)
                prior=env['reviews'].get(key)
                # The token includes the outcome references of THIS assessment,
                # so the first review is not immediately stale after saving.
                stub={'payload':json_text(clean)}
                env['reviews'][key]=stub
                clean['context_token']=self.token(env,*key)
                stamp=now()
                if prior and prior['input_token']==clean['context_token'] and json.loads(prior['payload'])==clean:
                    env['reviews'][key]=prior
                    results.append(dict(kind=key[0],id=key[1],unchanged=True,revision=prior['revision']))
                    continue
                revision=(prior['revision'] if prior else 0)+1
                c.execute('''INSERT INTO wk_followthrough_reviews VALUES(?,?,?,?,?,?)
                  ON CONFLICT(kind,record_id) DO UPDATE SET input_token=excluded.input_token,
                  payload=excluded.payload,revision=excluded.revision,reviewed_at=excluded.reviewed_at''',
                  (*key,clean['context_token'],json_text(clean),revision,stamp))
                saved=dict(kind=key[0],record_id=key[1],input_token=clean['context_token'],payload=json_text(clean),revision=revision,reviewed_at=stamp)
                c.execute('INSERT INTO wk_followthrough_events(kind,record_id,before_json,after_json,actor,happened_at) VALUES(?,?,?,?,?,?)',
                    (*key,json_text(prior or {}),json_text(saved),actor,stamp))
                env['reviews'][key]=saved
                results.append(dict(kind=key[0],id=key[1],revision=revision))
        return {'saved':results,'status':self.status()}

    def require_current(self, c, targets):
        if not self.enabled(c):
            return
        env=self.context(c)
        pending=[r for r in self.rows(c,env) if (r['kind'],r['id']) in targets and r['needs_review']]
        if pending:
            raise StoreError('本条资料相关的动作承接和关键未知尚未核对：'+'、'.join(r['name'] for r in pending[:5]),409)

    def detail(self, c, kind, rid):
        if kind not in KINDS or not self.enabled(c):
            return None
        env=self.context(c)
        record=env['records'][kind].get(rid)
        prior=env['reviews'].get((kind,rid))
        if not record or record.get('archived') or not prior:
            return None
        payload=json.loads(prior['payload'])
        return dict(payload,stale=prior['input_token']!=self.token(env,kind,rid),reviewed_at=prior['reviewed_at'],facet_labels=FACETS)
