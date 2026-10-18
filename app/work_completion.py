"""Evidence-backed work goals and mandatory consistency checks, not a classifier."""
from copy import deepcopy
import json
import re

from store import StoreError, json_text, now

KEY = 'work_completion_guard_version'
INSTRUCTIONS = (
    '先读取work-goal-packet，核对原始要求、范围修订和原文，保存有来源的原目标及完成条件。'
    '后续分析不能把认可、审批、补纪要等独立后续悄悄加为完成条件；目标变化须新依据和修订记录。'
    '每项相关工作分别提交completion_check，逐条判定原条件met/unmet/unknown并引用原文。'
    '全部满足必须关闭原工作，不能只写已发送或已会谈而保留待办；后续安排另行承接。'
    '多项工作的时间逐项绑定工作身份、具体写回内容和原句，不能共享一条全局下周安排。'
    '这些是Codex内部整理检查，不增加用户必填表单；原文歧义仍需语义判断，不按关键词自动关闭。'
)


class WorkCompletion:
    def __init__(self, store):
        self.store = store

    def initialize(self):
        with self.store.connect() as c:
            c.executescript('''
                CREATE TABLE IF NOT EXISTS wk_work_goals(
                  work_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                  payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS wk_work_goal_events(
                  id INTEGER PRIMARY KEY AUTOINCREMENT, work_id TEXT NOT NULL,
                  before_json TEXT NOT NULL, after_json TEXT NOT NULL,
                  reason TEXT NOT NULL, sources TEXT NOT NULL, happened_at TEXT NOT NULL);
            ''')

    def enable(self):
        self.initialize()
        with self.store.connect() as c:
            c.execute('INSERT OR REPLACE INTO wk_meta VALUES(?,?)', (KEY, '41'))
        return {'enabled': True, 'version': 41}

    @staticmethod
    def enabled(c):
        row = c.execute('SELECT value FROM wk_meta WHERE key=?', (KEY,)).fetchone()
        return bool(row and json.loads(row[0]) >= 41)

    @staticmethod
    def goal(c, rid):
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='wk_work_goals'").fetchone():
            return None
        row = c.execute('SELECT * FROM wk_work_goals WHERE work_id=?', (rid,)).fetchone()
        return dict(json.loads(row['payload']), revision=row['revision'], updated_at=row['updated_at']) if row else None

    @staticmethod
    def _text(value, label, limit=6000):
        if not isinstance(value, str) or not 2 <= len(value.strip()) <= limit:
            raise StoreError('请说明' + label + '。')
        return value.strip()

    def _refs(self, c, rid, refs, required=True):
        if not isinstance(refs, list) or len(refs) > 12 or (required and not refs):
            raise StoreError('完成条件和已发生结果须有可核对的来源。')
        from people import People
        result = []
        for ref in refs:
            if not isinstance(ref, dict):
                raise StoreError('依据格式无效。')
            if ref.get('type') != 'work_change':
                result.extend(People._refs(c, [ref]))
                continue
            # Manual work can have no external document. Its own immutable
            # maintenance event is a valid anchor; another work's event is not.
            field = ref.get('field')
            if field not in ('title', 'background', 'notes', 'progress', 'result', 'reason'):
                raise StoreError('工作目标须引用原要求，不能用未来下一步替代目标来源。')
            row = c.execute('SELECT * FROM wk_changes WHERE id=? AND kind=? AND record_id=?',
                            (ref.get('id'), 'tasks', rid)).fetchone()
            if not row:
                raise StoreError('目标引用的工作维护记录不属于本项工作。')
            content = (row['reason'] if field == 'reason' else
                       (json.loads(row['after_view']).get(field) or json.loads(row['after_payload']).get(field) or ''))
            quote = self._text(ref.get('quote'), '原要求摘录', 2000)
            if not isinstance(content, str) or quote not in content:
                raise StoreError('目标摘录与原工作维护记录不匹配。')
            result.append(dict(type='work_change', id=str(row['id']), field=field, quote=quote))
        return result

    def packet(self, rid):
        from work_analysis import WorkAnalysis, source_refs
        with self.store.connect() as c:
            record = self.store._record(c, 'tasks', rid)
            history = []
            for row in c.execute('SELECT * FROM wk_changes WHERE kind=? AND record_id=? ORDER BY id', ('tasks', rid)):
                before, after = json.loads(row['before_payload']), json.loads(row['after_payload'])
                if not history or any(before.get(k) != after.get(k) for k in ('title', 'background', 'notes')):
                    history.append(dict(change_id=row['id'], at=row['happened_at'], reason=row['reason'],
                        original={k: after.get(k) for k in ('title', 'background', 'notes', 'progress', 'schedule_note')}))
            return dict(id=rid, work_revision=record['revision'], goal=self.goal(c, rid),
                        context_token=WorkAnalysis(self.store).context_token(c, record),
                        work=record, origin_and_scope_changes=history, sources=source_refs(record), instructions=INSTRUCTIONS)

    def save_goal(self, document):
        required = {'id', 'revision', 'work_revision', 'context_token', 'goal', 'reason', 'change_sources'}
        if not isinstance(document, dict) or set(document) != required:
            raise StoreError('目标维护需当前工作和目标版本、上下文、完成条件、理由及范围变更依据。')
        from work_analysis import WorkAnalysis
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            rid = document['id']; record = self.store._record(c, 'tasks', rid)
            prior = self.goal(c, rid)
            if (type(document['revision']) is not int or type(document['work_revision']) is not int
                    or document['revision'] != (prior['revision'] if prior else 0)
                    or document['work_revision'] != record['revision']
                    or document['context_token'] != WorkAnalysis(self.store).context_token(c, record)):
                raise StoreError('工作或目标已变化，请重新核对原目标后保存。', 409)
            goal = deepcopy(document['goal'])
            if not isinstance(goal, dict) or set(goal) != {'summary', 'criteria'}:
                raise StoreError('目标需说明原目标和逐条完成条件。')
            goal['summary'] = self._text(goal['summary'], '原工作目标')
            if not isinstance(goal['criteria'], list) or not 1 <= len(goal['criteria']) <= 12:
                raise StoreError('请列出1至12条原有完成条件，不添加无依据的后续条件。')
            keys = set()
            for item in goal['criteria']:
                if not isinstance(item, dict) or set(item) != {'key', 'requirement', 'sources'}:
                    raise StoreError('每条完成条件需稳定标识、原要求和出处。')
                key = item['key']
                if not isinstance(key, str) or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', key) or key in keys:
                    raise StoreError('完成条件标识无效或重复。')
                keys.add(key)
                item['requirement'] = self._text(item['requirement'], '原有完成条件')
                item['sources'] = self._refs(c, rid, item['sources'])
            reason = self._text(document['reason'], '目标确认或修订理由')
            changes = self._refs(c, rid, document['change_sources'], required=False)
            if prior:
                old = {k: prior[k] for k in ('summary', 'criteria')}
                if old == goal:
                    return dict(id=rid, revision=prior['revision'], unchanged=True)
                prior_ids = {(r['type'], r['id']) for item in prior['criteria'] for r in item['sources']}
                if not changes or not any((r['type'], r['id']) not in prior_ids for r in changes):
                    raise StoreError('不能悄悄改大或缩小原目标；范围变更须有新增明确依据和修订理由。')
            revision = (prior['revision'] if prior else 0) + 1
            stamp = now()
            c.execute('INSERT OR REPLACE INTO wk_work_goals VALUES(?,?,?,?)', (rid, revision, json_text(goal), stamp))
            c.execute('INSERT INTO wk_work_goal_events(work_id,before_json,after_json,reason,sources,happened_at) VALUES(?,?,?,?,?,?)',
                      (rid, json_text(prior or {}), json_text(dict(goal, revision=revision)), reason, json_text(changes), stamp))
            return dict(id=rid, revision=revision, criteria=len(keys))

    def validate(self, c, record, check, *, action='analysis', changes=None, analysis=None):
        if not self.enabled(c):
            return check
        goal = self.goal(c, record['id'])
        if not goal:
            raise StoreError('请先核对并保存本项原目标和完成条件，再判断完成或下一步。', 409)
        required = {'goal_revision', 'criteria', 'outcome', 'follow_up', 'timing'}
        if not isinstance(check, dict) or set(check) - required - {'cancellation_sources'} or required - set(check):
            raise StoreError('须先逐条核对原完成条件、结果、独立后续和时间归属，不能只写一段判断理由。')
        clean = deepcopy(check)
        if type(check['goal_revision']) is not int or check['goal_revision'] != goal['revision']:
            raise StoreError('完成条件已修订，请按最新原目标重新判断。', 409)
        conditions = {i['key'] for i in goal['criteria']}
        if not isinstance(check['criteria'], list) or len(check['criteria']) != len(conditions):
            raise StoreError('须逐条核对全部原完成条件，不得遗漏或新增认可、审批等后续条件。')
        seen, statuses = set(), []
        for item in clean['criteria']:
            if not isinstance(item, dict) or set(item) != {'key', 'status', 'reason', 'sources'}:
                raise StoreError('每条完成判断须说明对应原条件、结果、理由和证据。')
            if item['key'] not in conditions or item['key'] in seen:
                raise StoreError('判断包含未约定的完成条件，或重复了同一条件。')
            seen.add(item['key'])
            if item['status'] not in ('met', 'unmet', 'unknown'):
                raise StoreError('原条件结果须区分已经满足、尚未满足和依据不足。')
            statuses.append(item['status'])
            item['reason'] = self._text(item['reason'], '完成条件判断理由')
            item['sources'] = self._refs(c, record['id'], item['sources'], required=item['status'] == 'met')
        derived = 'met' if all(s == 'met' for s in statuses) else 'unmet' if 'unmet' in statuses else 'unknown'
        if check['outcome'] == 'cancelled':
            clean['cancellation_sources'] = self._refs(c, record['id'], check.get('cancellation_sources'))
        elif check['outcome'] != derived or check.get('cancellation_sources'):
            raise StoreError('整体完成判断与原条件逐项结果不一致。')
        clean['follow_up'] = self._text(check['follow_up'], '后续事项是否独立于原目标')
        from work_analysis import CLOSED
        closed = record.get('state') in CLOSED
        target = (changes or {}).get('state', '已完成' if action == 'close' else record.get('state'))
        if action == 'close' and ((target == '已完成' and check['outcome'] != 'met')
                                  or (target == '已取消' and check['outcome'] != 'cancelled')):
            raise StoreError('只有原条件已满足才能判完成；取消须有明确取消依据。')
        if check['outcome'] in ('met', 'cancelled') and not closed and action not in ('close', 'decide'):
            # A protected human decision may leave closure pending. It must be
            # an actual visible closure question, not a prose-only exception.
            pending = any(op.get('kind') == 'tasks' and op.get('id') == record['id']
                          and op.get('values', {}).get('state') in CLOSED
                          for row in c.execute("SELECT payload FROM wk_judgment_cards WHERE state IN ('pending','deferred')")
                          for option in json.loads(row[0]).get('options', []) for op in option.get('operations', []))
            if action != 'analysis' or not pending:
                raise StoreError('已判断原目标完成或取消，须先同步工作状态；不能保留待办或仅改分析。')
        self._timing(c, record, clean, changes or {}, analysis, action)
        return clean

    def _timing(self, c, record, check, changes, analysis, action):
        from work_analysis import CLOSED
        values = {k: changes.get(k, record.get(k)) or '' for k in ('due_date', 'schedule_note', 'next_action')}
        required = {k for k in ('due_date', 'schedule_note', 'next_action') if k in changes}
        if analysis is not None:
            values.update(analysis_next_step=analysis.get('next_step', ''), review_on=analysis.get('review_on', ''))
            if record.get('state') not in CLOSED and action != 'close':
                required.update(k for k, v in values.items() if v)
        if not isinstance(check['timing'], list) or len(check['timing']) > 5:
            raise StoreError('时间须按本项工作逐项说明归属。')
        seen = set()
        for item in check['timing']:
            if not isinstance(item, dict) or set(item) != {'field', 'work_id', 'value', 'kind', 'sources', 'reason'}:
                raise StoreError('每项时间依据须绑定本工作、具体内容、约定或建议及原句。')
            field = item['field']
            if (field not in values or field in seen or item['work_id'] != record['id']
                    or item['value'] != values[field]):
                raise StoreError('时间归属或写回内容不匹配；不能把一项工作的时间套给另一项。')
            seen.add(field)
            kind = item['kind']
            if kind not in ('agreed', 'suggested', 'mixed', 'unspecified', 'cleared', 'historical'):
                raise StoreError('时间须区分原约定、建议、未确定及已发生时间。')
            if field == 'due_date' and values[field] and kind not in ('agreed', 'historical'):
                raise StoreError('建议或内部复核时间不能写为业务截止。')
            if field == 'review_on' and kind != 'suggested':
                raise StoreError('复核日是内部检查安排，不能当作业务承诺。')
            if field == 'analysis_next_step' and analysis.get('timing_kind') in ('agreed', 'mixed') and kind not in ('agreed', 'mixed'):
                raise StoreError('含约定的下一步须引用属于本工作的时间依据。')
            item['reason'] = self._text(item['reason'], '本项时间归属')
            item['sources'] = self._refs(c, record['id'], item['sources'], required=kind in ('agreed', 'mixed', 'historical'))
        if required - seen:
            raise StoreError('缺少本项工作的时间归属检查：' + '、'.join(sorted(required - seen)))
