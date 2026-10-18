"""Evidence-backed priorities for the user's attention at the current project stage.

Codex performs the semantic review. Rankings neither allocate resources nor
inherit urgency from the loudest linked task. Manual choices always win.
"""
from copy import deepcopy
import datetime as dt
import json
from pathlib import Path

from store import StoreError, json_text, now
from work_analysis import digest, source_refs
from work_priority import LEVELS, text

KEY = 'project_priority_version'
BASES = ('commitment', 'approved_focus', 'material_blocker', 'bounded_progress', 'confirmed_wait', 'tradeoff', 'manual')
FACETS = {'commitment': '已承接责任', 'value': '价值与复用', 'readiness': '推进条件',
          'effort': '投入边界', 'role': '与你的关系'}
POLICIES = ('docs/business-context.md',)
INSTRUCTIONS = (
    '判断你当前应重点推动的项目顺序。先读当前项目、关联工作的最新进展、用户决定和原文，明确适用阶段与投入边界。'
    '保障已承接责任；依据已确认方向、真实影响及关键阻塞判断高优先级；明确且有边界的阶段行动通常为普通；'
    '只有明确等待、暂缓、拒绝或本人仅关注且无主动动作才为低。缺少收益/预算/成本不等于低。'
    '分别说明责任、价值、推进条件、投入边界及本人角色的已知和未知；历史计划不等于当前逾期，客户预算不等于利润。'
    '不按客户名、项目金额、消息数量或最高工作优先级打分，不将项目优先级传播为全部工作的紧急程度。'
    '能确定的当前阶段先判断；扩大范围/追加资源的未知单独说明，不据此否定已有交付责任。'
    '只有改变当前排序的关键未知才关联可见问题，优先复用已有问题，不生成全项目问卷。'
    '保存project-priority-review，人工设定优先，七天内复核；新依据、项目/工作变化或用户决定触发重看。'
    '优先级只影响展示顺序，不批准投入、不更改工作状态、负责人、期限或提醒。'
)


class ProjectPriority:
    def __init__(self, store):
        self.store = store

    def initialize(self):
        with self.store.connect() as c:
            c.executescript('''
              CREATE TABLE IF NOT EXISTS wk_project_priority_reviews(
                project_id TEXT PRIMARY KEY, revision INTEGER NOT NULL, context_token TEXT NOT NULL,
                payload TEXT NOT NULL, request_hash TEXT NOT NULL, reviewed_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS wk_project_priority_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL,
                before_json TEXT NOT NULL, after_json TEXT NOT NULL, actor TEXT NOT NULL, happened_at TEXT NOT NULL);
            ''')

    def enable(self):
        self.initialize()
        with self.store.connect() as c:
            c.execute('INSERT OR REPLACE INTO wk_meta VALUES(?,?)', (KEY, '43'))
        return dict(enabled=True, version=43)

    @staticmethod
    def enabled(c):
        row = c.execute('SELECT value FROM wk_meta WHERE key=?', (KEY,)).fetchone()
        return bool(row and json.loads(row[0]) >= 43)

    @staticmethod
    def manual(r):
        p = r.get('_provenance', {}).get('priority') or {}
        return r.get('priority') in LEVELS and (p.get('mode') == 'user' or not p)

    def context(self, c, env=None):
        from follow_through import FollowThrough
        env = dict(env) if env is not None else FollowThrough(self.store).context(c)
        env['project_priorities'] = {r['project_id']: dict(r) for r in c.execute('SELECT * FROM wk_project_priority_reviews')}
        root = Path(__file__).resolve().parents[1]
        env['priority_policies'] = {p: (root / p).read_text('utf-8-sig') if (root / p).is_file() else '' for p in POLICIES}
        return env

    @staticmethod
    def token(env, r, assessment=None):
        refs = (assessment or {}).get('sources', [])
        linked = sorted((wid, env['hashes'][('tasks', wid)]) for wid, w in env['records']['tasks'].items() if w.get('project_id') == r['id'])
        cards = [env['cards'][qid] for qid in env['by_target'].get(('projects', r['id']), [])]
        return digest([env['hashes'].get(('projects', r['id'])), linked, cards,
                       r.get('priority'), r.get('_provenance', {}).get('priority'),
                       sorted((s['id'], env['raw'].get(s['id'])) for s in refs if s['type'] == 'raw'),
                       sorted((s['id'], digest(env['priority_policies'].get(s['id'], ''))) for s in refs if s['type'] == 'policy')])

    def view(self, c, r, env=None, today=None, compact=False):
        closed = bool(r.get('archived') or r.get('state') == '已完成')
        manual = self.manual(r)
        if not self.enabled(c):
            return dict(priority=r.get('priority') or '', mode='manual' if manual else 'legacy',
                        needs_review=False, needs_user=False, closed=closed, reason='')
        env = env if env is not None else self.context(c)
        row = env['project_priorities'].get(r['id'])
        a = json.loads(row['payload']) if row else {}
        changed = bool(row and row['context_token'] != self.token(env, r, a))
        due = bool(a.get('review_on') and a['review_on'] <= (today or now()[:10]))
        questions = [dict(id=qid, question=env['cards'][qid]['question'], state=env['cards'][qid]['state'])
                     for qid in a.get('judgment_ids', []) if qid in env['cards']]
        needs_user = bool(not closed and a.get('outcome') == 'needs_user' and any(q['state'] in ('pending', 'deferred') for q in questions))
        priority = r.get('priority') if manual else a.get('priority', '')
        mode = 'manual' if manual else 'needs_user' if needs_user else a.get('outcome', 'unreviewed')
        if closed:
            priority, mode = '', 'closed'
        reason = a.get('reason', '')
        if manual:
            reason = '按你明确设置的项目优先级执行。' + ((' 最近核对：' + reason) if reason else '')
        result = dict(priority=priority or '', mode=mode, closed=closed, needs_user=needs_user,
                      needs_review=not closed and (not row or changed or due), reason=reason, scope=a.get('scope', ''),
                      review_reason='项目、关联工作、依据或决定有变化' if changed else '已到复核日' if due else '尚未判断' if not row else '',
                      review_on=a.get('review_on', ''), reviewed_at=row['reviewed_at'] if row else '')
        if not compact:
            result.update(revision=row['revision'] if row else 0, context_token=self.token(env, r, a),
                          sources=a.get('sources', []), facets=a.get('facets', {}),
                          limits=a.get('limits', ''), questions=questions, basis=a.get('basis', ''))
        return result

    def views(self, c, records, env=None):
        env = self.context(c, env) if self.enabled(c) else None
        return {r['id']: self.view(c, r, env, compact=True) for r in records}

    def status(self, c=None):
        if c is None:
            with self.store.connect() as conn:
                conn.execute('BEGIN')
                return self.status(conn)
        values = self.views(c, self.store._records(c, 'projects').values()).values()
        active = [v for v in values if not v['closed']]
        return dict(enabled=self.enabled(c), active=len(active), pending=sum(v['needs_review'] for v in active),
                    needs_user=sum(v['needs_user'] for v in active), counts={k: sum(v['priority'] == k for v in active) for k in (*LEVELS, '')})

    def packet(self, limit=20, ids=None):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise StoreError('每批核对1至200个项目。')
        with self.store.connect() as c:
            c.execute('BEGIN')
            if not self.enabled(c):
                return dict(enabled=False, pending=0, items=[], instructions=INSTRUCTIONS)
            env = self.context(c)
            if ids and any(rid not in env['records']['projects'] for rid in ids):
                raise StoreError('项目不存在。', 404)
            items, pending = [], 0
            for r in env['records']['projects'].values():
                v = self.view(c, r, env)
                if v['closed']:
                    continue
                pending += int(v['needs_review'])
                if len(items) >= limit or (r['id'] not in ids if ids else not v['needs_review']):
                    continue
                works = [w for w in env['records']['tasks'].values() if w.get('project_id') == r['id']]
                items.append(dict(id=r['id'], project_revision=r['revision'], revision=v['revision'],
                    context_token=v['context_token'], project=r, current=v, works=works,
                    sources=source_refs(r), questions=[env['cards'][qid] for qid in env['by_target'].get(('projects', r['id']), [])]))
            return dict(pending=pending, items=items, instructions=INSTRUCTIONS)

    def _refs(self, c, rid, refs, env):
        from people import People
        if not isinstance(refs, list) or not 1 <= len(refs) <= 8:
            raise StoreError('项目判断须有1至8条可核对依据。')
        result = []
        for ref in refs:
            if not isinstance(ref, dict):
                raise StoreError('项目判断依据格式错误。')
            if ref.get('type') == 'project_change':
                field = ref.get('field')
                if field not in ('my_role', 'decision', 'decision_reason', 'owner', 'priority', 'stage', 'notes', 'reason'):
                    raise StoreError('项目维护依据须引用本项实际决定或分工。')
                row = c.execute('SELECT * FROM wk_changes WHERE id=? AND kind=? AND record_id=?',
                                (ref.get('id'), 'projects', rid)).fetchone()
                content = (row['reason'] if field == 'reason' else
                           (json.loads(row['after_view']).get(field) or json.loads(row['after_payload']).get(field) or '')) if row else ''
                quote = ref.get('quote', '')
                if not isinstance(content, str) or not isinstance(quote, str) or not quote.strip() or quote not in content:
                    raise StoreError('项目维护摘录与本项历史记录不匹配。')
                result.append(dict(type='project_change', id=str(row['id']), field=field, quote=quote,
                                   title='项目维护记录', event_at=row['happened_at']))
                continue
            if ref.get('type') != 'policy':
                result += People._refs(c, [ref])
                continue
            body = env['priority_policies'].get(ref.get('id'))
            quote = ref.get('quote', '')
            if not body or not isinstance(quote, str) or not quote.strip() or quote not in body:
                raise StoreError('方向依据须来自当前已确认的项目口径，并保留真实原句。')
            result.append(dict(type='policy', id=ref['id'], quote=quote, sha256=digest(body), title=Path(ref['id']).stem))
        return result

    def _validate(self, c, r, item, env):
        required = {'id', 'project_revision', 'revision', 'context_token', 'assessed_on', 'outcome', 'priority',
                    'basis', 'reason', 'scope', 'facets', 'limits', 'sources', 'review_on', 'judgment_ids'}
        if not isinstance(item, dict) or set(item) - required - {'question'} or required - set(item):
            raise StoreError('项目优先级须包含当前版本、阶段范围、判断理由、已知与未知及依据。')
        a = deepcopy({k: item[k] for k in required - {'id', 'project_revision', 'revision', 'context_token'}})
        today = dt.date.fromisoformat(now()[:10])
        try:
            review_on = dt.date.fromisoformat(a['review_on'])
        except (ValueError, TypeError):
            raise StoreError('请提供有效的复核日期。') from None
        if a['assessed_on'] != today.isoformat() or not today < review_on <= today + dt.timedelta(days=7):
            raise StoreError('须按今天的情况判断，并在未来七天内复核。')
        if a['outcome'] not in ('automatic', 'needs_user', 'manual') or a['priority'] not in (*LEVELS, '') or a['basis'] not in BASES:
            raise StoreError('项目优先级或判断方式无效。')
        for key in ('reason', 'scope', 'limits'):
            a[key] = text(a[key], {'reason':'优先级理由','scope':'当前阶段和投入范围','limits':'尚不能据此决定的事项'}[key])
        if not isinstance(a['facets'], dict) or set(a['facets']) != set(FACETS):
            raise StoreError('请分别核对已承接责任、价值、推进条件、投入边界和本人角色。')
        for key, f in a['facets'].items():
            if not isinstance(f, dict) or set(f) != {'known', 'unknown'} or not all(isinstance(v, str) for v in f.values()) or not any(v.strip() for v in f.values()):
                raise StoreError(FACETS[key] + '须说明已知情况或真实未知，不能用空白冒充已核对。')
            if any(len(v) > 3000 for v in f.values()):
                raise StoreError('项目判断说明过长。')
        a['sources'] = self._refs(c, r['id'], a['sources'], env)
        if a['outcome'] == 'automatic':
            if a['priority'] not in LEVELS or a['basis'] in ('manual', 'tradeoff'):
                raise StoreError('直接判断须有明确等级和当前业务依据。')
            if self.manual(r) and a['priority'] != r['priority']:
                raise StoreError('不能覆盖你明确设置的项目优先级。', 409)
            if a['priority'] == '高' and a['basis'] not in ('commitment', 'approved_focus', 'material_blocker'):
                raise StoreError('高优先级须有已承接责任、已确认重点或重要阻塞依据。')
            if a['priority'] == '低' and a['basis'] != 'confirmed_wait':
                raise StoreError('低优先级须有明确等待或本人无主动推进责任的依据，不能因为缺信息而降低。')
        elif a['outcome'] == 'manual':
            if not self.manual(r) or a['priority'] != r['priority'] or a['basis'] != 'manual':
                raise StoreError('人工设定须对应当前真实的用户决定。')
        elif a['basis'] != 'tradeoff' or a['priority']:
            raise StoreError('当前排序无法判断时保留空白，并明确需要你决定的取舍。')
        qids = a['judgment_ids']
        if not isinstance(qids, list) or len(qids) > 4 or any(not isinstance(qid, str) for qid in qids):
            raise StoreError('请关联本项目实际需要判断的问题。')
        if a['outcome'] != 'needs_user' and (qids or item.get('question')):
            raise StoreError('可直接判断当前阶段时，范围外未知写入判断边界，不冒充排序待决。')
        if item.get('question'):
            from judgment_cards import JudgmentCards
            card = item['question']
            expected = 'project-priority-v43-' + digest(r['id'])[:24]
            if card.get('id') != expected:
                raise StoreError('同项目排序问题须复用固定身份，不能换名重问。')
            for option in card.get('options', []):
                ops = option.get('operations', [])
                if len(ops) != 1 or ops[0].get('kind') != 'projects' or ops[0].get('id') != r['id'] or set(ops[0].get('values', {})) != {'priority'}:
                    raise StoreError('项目优先级选项只能调整本项目的优先级。')
            saved = JudgmentCards(self.store).propose(card, 'Codex项目优先级判断', connection=c)
            if saved.get('protected'):
                raise StoreError('该问题已有人工决定，不能自动重开。', 409)
            qids = list(dict.fromkeys([*qids, expected]))
        if a['outcome'] == 'needs_user':
            if not qids:
                raise StoreError('关键取舍须有可见问题，优先关联已有问题。')
            for qid in qids:
                row = c.execute('SELECT * FROM wk_judgment_cards WHERE id=?', (qid,)).fetchone()
                if not row or row['state'] not in ('pending', 'deferred'):
                    raise StoreError('关联问题已处理或不可用，请重新核对。', 409)
                targets = {(op['kind'], op['id']) for o in json.loads(row['payload'])['options'] for op in o['operations']}
                if ('projects', r['id']) not in targets:
                    raise StoreError('关联问题不属于本项目。')
        a['judgment_ids'] = list(dict.fromkeys(qids))
        return a

    def review(self, document):
        if not isinstance(document, dict) or set(document) != {'items'} or not isinstance(document['items'], list) or not 1 <= len(document['items']) <= 200:
            raise StoreError('请逐项提交项目优先级判断。')
        results, seen = [], set()
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not self.enabled(c):
                raise StoreError('项目优先级机制尚未启用。')
            env = self.context(c)
            for item in document['items']:
                rid = item.get('id') if isinstance(item, dict) else None
                if rid in seen or rid not in env['records']['projects']:
                    raise StoreError('项目不存在或本批重复。')
                seen.add(rid)
                r = env['records']['projects'][rid]
                if r.get('archived') or r.get('state') == '已完成':
                    raise StoreError('已结束项目不参加当前排序。', 409)
                row = env['project_priorities'].get(rid)
                prior = json.loads(row['payload']) if row else {}
                token = self.token(env, r, prior)
                if row and row['request_hash'] == digest(item) and row['context_token'] == token and prior.get('assessed_on') == now()[:10]:
                    results.append(dict(id=rid, revision=row['revision'], unchanged=True))
                    continue
                if (type(item.get('revision')) is not int or item['revision'] != (row['revision'] if row else 0)
                        or type(item.get('project_revision')) is not int or item['project_revision'] != r['revision']
                        or item.get('context_token') != token):
                    raise StoreError('项目、关联工作或决定已有变化，请重新读取再判断。', 409)
                a = self._validate(c, r, item, env)
                if item.get('question'):
                    env = self.context(c)
                token = self.token(env, r, a)
                if row and row['context_token'] == token and prior == a:
                    results.append(dict(id=rid, revision=row['revision'], unchanged=True))
                    continue
                revision, stamp = (row['revision'] if row else 0) + 1, now()
                after = dict(project_id=rid, revision=revision, context_token=token, payload=json_text(a), request_hash=digest(item), reviewed_at=stamp)
                c.execute('INSERT OR REPLACE INTO wk_project_priority_reviews VALUES(?,?,?,?,?,?)', tuple(after.values()))
                c.execute('INSERT INTO wk_project_priority_events(project_id,before_json,after_json,actor,happened_at) VALUES(?,?,?,?,?)',
                          (rid, json_text(row or {}), json_text(after), 'Codex项目优先级判断', stamp))
                env['project_priorities'][rid] = after
                results.append(dict(id=rid, revision=revision, outcome=a['outcome'], priority=a['priority']))
        return dict(items=results, saved=sum(not r.get('unchanged') for r in results))

    def require_current(self, c, targets):
        if not self.enabled(c):
            return
        env = self.context(c)
        ids = {rid for kind, rid in targets if kind == 'projects'}
        ids.update(env['records']['tasks'].get(rid, {}).get('project_id') for kind, rid in targets if kind == 'tasks')
        missing = [rid for rid in ids if rid in env['records']['projects'] and self.view(c, env['records']['projects'][rid], env)['needs_review']]
        if missing:
            raise StoreError('相关项目须先核对当前优先级，再登记整理完成：' + '、'.join(sorted(missing)), 409)

    def history(self, c, rid):
        if not self.enabled(c):
            return []
        return [dict(id=r['id'], at=r['happened_at'], assessment=json.loads(json.loads(r['after_json'])['payload']))
                for r in c.execute('SELECT * FROM wk_project_priority_events WHERE project_id=? ORDER BY id DESC LIMIT 20', (rid,))]
