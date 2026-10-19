"""Reviewed, evidence-backed ordering. Never changes business facts or dates.

Codex supplies the semantic assessment. This service validates its evidence,
protects manual choices, and keeps stale/uncertain decisions visible.
"""
from copy import deepcopy
import datetime as dt
import json

from store import StoreError, json_text, now
from work_analysis import CLOSED, digest, source_refs

KEY = 'work_priority_version'
LEVELS = ('高', '普通', '低')
BASES = ('near_commitment', 'active_blocker', 'material_impact', 'confirmed_wait',
         'routine', 'tradeoff', 'manual')
INSTRUCTIONS = (
    '每轮整理先完成事实、状态和下一步，再读取work-priority-packet；有新记录时对所有受影响工作核对优先级，'
    '即使没有新聊天也检查到期复核项。结合原目标、当前动作、约定时间、实际阻塞及影响判断，'
    '不能按关键词、消息数量、客户名称或内部复核日机械打分。'
    '高：三天内明确约定、当前关键阻塞或有依据的重大影响；普通：正常推进，无需抢占当前关键工作；'
    '低：已确认暂缓或等待，当前无主动动作。等反馈不一律降级，缺信息不等于低。'
    '计划、预计、建议及历史时间分开，旧日期过去不证明逾期。原人工优先级优先，'
    '冲突或关键取舍只提具体问题；复用已有问题，已处理或稍后处理不自动重开。'
    '通过work-priority-review保存每项理由、来源和复核日期。优先级只影响展示排序，'
    '不替换工作分工、完成条件、截止或提醒，不新增用户问卷。'
)


def text(value, label):
    if not isinstance(value, str) or not 4 <= len(value.strip()) <= 6000:
        raise StoreError('请说明' + label + '。')
    return value.strip()


class WorkPriority:
    def __init__(self, store):
        self.store = store

    def initialize(self):
        with self.store.connect() as c:
            c.executescript('''
              CREATE TABLE IF NOT EXISTS wk_work_priority_reviews(
                work_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                context_token TEXT NOT NULL, payload TEXT NOT NULL,
                request_hash TEXT NOT NULL, reviewed_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS wk_work_priority_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, work_id TEXT NOT NULL,
                before_json TEXT NOT NULL, after_json TEXT NOT NULL,
                actor TEXT NOT NULL, happened_at TEXT NOT NULL);
            ''')

    def enable(self):
        self.initialize()
        with self.store.connect() as c:
            c.execute('INSERT OR REPLACE INTO wk_meta VALUES(?,?)', (KEY, '42'))
        return dict(enabled=True, version=42)

    @staticmethod
    def enabled(c):
        row = c.execute('SELECT value FROM wk_meta WHERE key=?', (KEY,)).fetchone()
        return bool(row and json.loads(row[0]) >= 42)

    @staticmethod
    def manual(record):
        p = record.get('_provenance', {}).get('priority') or {}
        return record.get('priority') in LEVELS and (p.get('mode') == 'user' or not p)

    def context(self, c, env=None):
        from follow_through import FollowThrough
        env = dict(env) if env is not None else FollowThrough(self.store).context(c)
        env['priority'] = {r['work_id']: dict(r) for r in c.execute('SELECT * FROM wk_work_priority_reviews')}
        return env

    @staticmethod
    def token(env, record, assessment=None):
        if env.get('incremental'):
            from incremental import priority_token
            return priority_token(env, 'tasks', record, assessment or {})
        refs = list((assessment or {}).get('sources', []))
        refs += (assessment or {}).get('timing', {}).get('sources', [])
        return digest([env['hashes'].get(('tasks', record['id'])),
                       env['hashes'].get(('projects', record.get('project_id'))),
                       sorted((s['id'], env['raw'].get(s['id'])) for s in refs if s['type'] == 'raw')])

    def view(self, c, record, env=None, today=None):
        today = today or now()[:10]
        closed = record.get('archived') or record.get('state') in CLOSED
        manual = self.manual(record)
        if not self.enabled(c):
            return dict(priority=record.get('priority') or '', mode='manual' if manual else 'legacy',
                        needs_review=False, needs_user=False, reason='', closed=bool(closed))
        env = env or self.context(c)
        row = env['priority'].get(record['id'])
        a = json.loads(row['payload']) if row else {}
        token = self.token(env, record, a)
        changed = bool(row and row['context_token'] != token)
        time_due = bool(a.get('review_on') and a['review_on'] <= today)
        if env.get('incremental'):
            time_due = False
        stale = not closed and (not row or changed or time_due)
        if env.get('incremental') and manual:
            stale = False
        questions = [dict(id=qid, question=env['cards'][qid]['question'], state=env['cards'][qid]['state'])
                     for qid in a.get('judgment_ids', []) if qid in env['cards']]
        needs_user = bool(not closed and a.get('outcome') == 'needs_user'
                          and any(q['state'] in ('pending', 'deferred') for q in questions))
        priority = record.get('priority') or ''
        mode = 'manual' if manual else 'legacy'
        if not manual and a.get('outcome') == 'automatic':
            priority, mode = a['priority'], 'automatic'
        elif not manual and needs_user:
            mode = 'needs_user'
        if closed:
            priority, mode = '', 'closed'
        reason = a.get('reason', '')
        if manual:
            reason = '按你明确设置的优先级执行。' + ((' 最近核对：' + reason) if reason else '')
        return dict(priority=priority, mode=mode, needs_review=stale, needs_user=needs_user,
                    closed=bool(closed), reason=reason, sources=a.get('sources', []), questions=questions,
                    review_on=a.get('review_on', ''), reviewed_at=row['reviewed_at'] if row else '',
                    revision=row['revision'] if row else 0, context_token=token,
                    review_reason='工作、依据或决定有变化' if changed else '已到复核日' if time_due else '尚未判断' if not row else '')

    def views(self, c, records, env=None):
        env = self.context(c, env) if self.enabled(c) else None
        return {r['id']: self.view(c, r, env) for r in records}

    def status(self, c=None):
        if c is None:
            with self.store.connect() as conn:
                conn.execute('BEGIN')
                return self.status(conn)
        records = list(self.store._records(c, 'tasks').values())
        views = self.views(c, records)
        active = [v for v in views.values() if not v['closed']]
        return dict(enabled=self.enabled(c), active=len(active), pending=sum(v['needs_review'] for v in active),
                    needs_user=sum(v['needs_user'] for v in active),
                    counts={level: sum(v['priority'] == level for v in active) for level in (*LEVELS, '')})

    def packet(self, limit=20, ids=None):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise StoreError('每批核对1至200项工作。')
        with self.store.connect() as c:
            c.execute('BEGIN')
            if not self.enabled(c):
                return dict(pending=0, items=[], instructions=INSTRUCTIONS, enabled=False)
            env = self.context(c)
            records = env['records']['tasks']
            if ids and any(rid not in records for rid in ids):
                raise StoreError('工作不存在。', 404)
            me = json.loads(c.execute("SELECT value FROM wk_meta WHERE key='current_user_id'").fetchone()[0])
            items, pending = [], 0
            for r in sorted(records.values(), key=lambda r: (not (r.get('owner_id') == me or me in (r.get('participant_ids') or [])), r['id'])):
                v = self.view(c, r, env)
                if v['closed']:
                    continue
                pending += int(v['needs_review'])
                if len(items) >= limit or (r['id'] not in ids if ids else not v['needs_review']):
                    continue
                qids = set(env['by_target'].get(('tasks', r['id']), [])
                           + env['by_target'].get(('projects', r.get('project_id')), []))
                items.append(dict(id=r['id'], work_revision=r['revision'], revision=v['revision'],
                    context_token=v['context_token'], work=r, current=v,
                    project=env['records']['projects'].get(r.get('project_id'), {}),
                    sources=source_refs(r), questions=[env['cards'][qid] for qid in sorted(qids)]))
            return dict(pending=pending, items=items, instructions=__import__('incremental').PRIORITY_INSTRUCTIONS if env.get('incremental') else INSTRUCTIONS)

    def _refs(self, c, rid, refs):
        # The goal service accepts immutable source versions and this work's own
        # historical edits. Priority is a derived judgment, not a source fact.
        from work_completion import WorkCompletion
        return WorkCompletion(self.store)._refs(c, rid, refs)

    def _validate(self, c, r, item, env):
        required = {'id', 'work_revision', 'revision', 'context_token', 'assessed_on', 'outcome',
                    'priority', 'basis', 'reason', 'sources', 'timing', 'review_on', 'judgment_ids'}
        if not isinstance(item, dict) or set(item) - required - {'question'} or required - set(item):
            raise StoreError('优先级核对需当前版本、判断、理由、来源、时间性质和复核日。')
        a = deepcopy({k: item[k] for k in required - {'id', 'work_revision', 'revision', 'context_token'}})
        today = dt.date.fromisoformat(now()[:10])
        incremental = __import__('incremental').enabled(c)
        try:
            review_on = dt.date.fromisoformat(a['review_on']) if a['review_on'] else None
        except (ValueError, TypeError):
            raise StoreError('请提供有效的优先级复核日期。') from None
        if a['assessed_on'] != today.isoformat() or (not incremental and (not review_on or not today < review_on <= today + dt.timedelta(days=7))):
            raise StoreError('按今天的真实情况判断，并在未来七天内复核；复核日不是业务截止。')
        if a['outcome'] not in ('automatic', 'needs_user', 'manual') or a['basis'] not in BASES:
            raise StoreError('请区分直接判断、待你判断和保留人工设定。')
        if a['priority'] not in (*LEVELS, ''):
            raise StoreError('优先级为高、普通或低；无法判断时不填。')
        a['reason'] = text(a['reason'], '本项优先级判断理由')
        a['sources'] = self._refs(c, r['id'], a['sources'])
        t = a['timing']
        if not isinstance(t, dict) or set(t) != {'kind', 'date', 'reason', 'sources'} or t['kind'] not in ('agreed', 'planned', 'suggested', 'historical', 'none'):
            raise StoreError('须区分本项的明确约定、计划、建议、历史日期和没有日期。')
        t['reason'] = text(t['reason'], '本项时间依据')
        if t['date']:
            try:
                dt.date.fromisoformat(t['date'])
            except (ValueError, TypeError):
                raise StoreError('时间核对的日期无效。') from None
        t['sources'] = self._refs(c, r['id'], t['sources']) if t['kind'] != 'none' else []
        if t['kind'] == 'none' and t['date']:
            raise StoreError('未定日期不能填入一个确定日期。')
        if a['outcome'] == 'automatic':
            if a['priority'] not in LEVELS:
                raise StoreError('直接判断须有明确优先级。')
            if self.manual(r) and a['priority'] != r['priority']:
                raise StoreError('不能覆盖人工优先级；有实质冲突时提交具体判断问题。', 409)
            if a['basis'] in ('tradeoff', 'manual'):
                raise StoreError('关键取舍不能伪装为直接判断。')
            if a['priority'] == '高' and a['basis'] not in ('near_commitment', 'active_blocker', 'material_impact'):
                raise StoreError('高优先级须有临近约定、当前关键阻塞或重大影响依据。')
            if a['basis'] == 'near_commitment':
                if t['kind'] != 'agreed' or not t['date'] or not 0 <= (dt.date.fromisoformat(t['date']) - today).days <= 3:
                    raise StoreError('临近约定须是未来三天内本项的明确安排，建议或历史日期不能代替。')
            if a['priority'] == '低' and a['basis'] != 'confirmed_wait':
                raise StoreError('低优先级须有已确认暂缓或等待依据；不能因为缺信息而降低。')
            if a['basis'] == 'confirmed_wait' and t['kind'] == 'agreed' and t['date'] and 0 <= (dt.date.fromisoformat(t['date']) - today).days <= 3:
                raise StoreError('临近约定与等待有冲突，请先核对或交由用户判断。')
        elif a['outcome'] == 'manual':
            if not self.manual(r) or a['priority'] != r['priority'] or a['basis'] != 'manual':
                raise StoreError('保留人工设定必须对应当前真实的人工优先级。')
        elif a['basis'] != 'tradeoff':
            raise StoreError('待你判断须说明影响排序的实际取舍或关键未知。')
        qids = a['judgment_ids']
        if not isinstance(qids, list) or any(not isinstance(qid, str) for qid in qids) or len(qids) > 4:
            raise StoreError('请关联本项实际未解决的问题。')
        if a['outcome'] != 'needs_user' and (qids or item.get('question')):
            raise StoreError('已有明确结论不应附带未解决的优先级问题。')
        if item.get('question'):
            from judgment_cards import JudgmentCards
            card = item['question']
            expected = 'priority-v42-' + digest(r['id'])[:24]
            if card.get('id') != expected:
                raise StoreError('同项优先级取舍必须复用固定问题身份，不能换名重问。')
            for option in card.get('options', []):
                if len(option.get('operations', [])) != 1:
                    raise StoreError('优先级问题只调整本项的优先级。')
                op = option['operations'][0]
                if op.get('kind') != 'tasks' or op.get('id') != r['id'] or set(op.get('values', {})) != {'priority'}:
                    raise StoreError('优先级选项不能改写状态、责任或日期。')
            saved = JudgmentCards(self.store).propose(card, 'Codex优先级判断', connection=c)
            if saved.get('protected'):
                raise StoreError('该问题已有人工决定，不能自动重开；请读取决定后核对。', 409)
            qids = list(dict.fromkeys([*qids, expected]))
        if a['outcome'] == 'needs_user':
            if not qids:
                raise StoreError('无法判断时须有可见问题；优先复用已有问题。')
            for qid in qids:
                row = c.execute('SELECT * FROM wk_judgment_cards WHERE id=?', (qid,)).fetchone()
                if not row or row['state'] not in ('pending', 'deferred'):
                    raise StoreError('关联问题已处理或不可用，不能声称仍待用户决定。', 409)
                card = json.loads(row['payload'])
                targets = {(op['kind'], op['id']) for o in card['options'] for op in o['operations']}
                if not {('tasks', r['id']), ('projects', r.get('project_id'))} & targets:
                    raise StoreError('该问题与本项工作没有关联。')
        a['judgment_ids'] = list(dict.fromkeys(qids))
        return a

    def review(self, document):
        if not isinstance(document, dict) or set(document) != {'items'} or not isinstance(document['items'], list) or not 1 <= len(document['items']) <= 200:
            raise StoreError('请逐项提交本批优先级判断。')
        results, seen = [], set()
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not self.enabled(c):
                raise StoreError('优先级判断尚未启用。')
            env = self.context(c)
            for item in document['items']:
                rid = item.get('id') if isinstance(item, dict) else None
                if rid in seen or rid not in env['records']['tasks']:
                    raise StoreError('工作不存在或在本批中重复。')
                seen.add(rid)
                r = env['records']['tasks'][rid]
                if r.get('archived') or r.get('state') in CLOSED:
                    raise StoreError('已结束的工作不参与当前优先级排序。', 409)
                row = env['priority'].get(rid)
                prior = json.loads(row['payload']) if row else {}
                token = self.token(env, r, prior)
                if row and row['request_hash'] == digest(item) and row['context_token'] == token and prior.get('assessed_on') == now()[:10]:
                    results.append(dict(id=rid, revision=row['revision'], unchanged=True))
                    continue
                if (type(item.get('revision')) is not int or item['revision'] != (row['revision'] if row else 0)
                        or type(item.get('work_revision')) is not int or item['work_revision'] != r['revision']
                        or item.get('context_token') != token):
                    raise StoreError('工作、依据、问题或人工决定已有变化，请重新读取再判断。', 409)
                a = self._validate(c, r, item, env)
                env = self.context(c)  # Includes a newly saved question, if any.
                token = self.token(env, r, a)
                if row and row['context_token'] == token and prior == a:
                    results.append(dict(id=rid, revision=row['revision'], unchanged=True))
                    continue
                revision, stamp = (row['revision'] if row else 0) + 1, now()
                after = dict(work_id=rid, revision=revision, context_token=token,
                             payload=json_text(a), request_hash=digest(item), reviewed_at=stamp)
                c.execute('INSERT OR REPLACE INTO wk_work_priority_reviews VALUES(?,?,?,?,?,?)', tuple(after.values()))
                c.execute('INSERT INTO wk_work_priority_events(work_id,before_json,after_json,actor,happened_at) VALUES(?,?,?,?,?)',
                          (rid, json_text(row or {}), json_text(after), 'Codex优先级判断', stamp))
                env['priority'][rid] = after
                results.append(dict(id=rid, revision=revision, outcome=a['outcome'], priority=a['priority']))
        return dict(items=results, saved=sum(not r.get('unchanged') for r in results))

    def require_current(self, c, targets):
        if not self.enabled(c):
            return
        env = self.context(c)
        ids = {rid for kind, rid in targets if kind == 'tasks'}
        project_ids = {rid for kind, rid in targets if kind == 'projects'}
        ids.update(r['id'] for r in env['records']['tasks'].values() if r.get('project_id') in project_ids)
        missing = [rid for rid in ids if rid in env['records']['tasks']
                   and self.view(c, env['records']['tasks'][rid], env)['needs_review']]
        if missing:
            raise StoreError('相关工作须先完成优先级核对，再登记本批整理完成：' + '、'.join(missing), 409)

    def history(self, c, rid):
        if not self.enabled(c):
            return []
        return [dict(id=r['id'], at=r['happened_at'], assessment=json.loads(json.loads(r['after_json'])['payload']))
                for r in c.execute('SELECT * FROM wk_work_priority_events WHERE work_id=? ORDER BY id DESC LIMIT 20', (rid,))]
