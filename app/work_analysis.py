"""Reviewed work analysis shared by Codex, the workbench and periodic review.

This module stores a supplied, evidence-backed analysis. It does not call a
model or turn a source keyword into an assignment, deadline or completion.
"""
from copy import deepcopy
from contextlib import nullcontext
import datetime as dt
import hashlib
import json

from model import FIELDS
from store import StoreError, json_text, now
from work_model import references

CLOSED = {'已完成', '已取消'}
ANALYSIS_FIELDS = {'current', 'next_step', 'next_key', 'timing_kind', 'timing_basis',
                   'review_on', 'rationale', 'sources', 'context_token'}


def digest(value):
    return hashlib.sha256(json_text(value).encode('utf-8')).hexdigest()


def source_refs(record):
    refs = references(record)
    for item in record.get('work_log') or []:
        refs.extend(item.get('sources') or [])
    refs.extend((record.get('_work_analysis') or {}).get('sources') or [])
    return list({(s.get('type'), s.get('id'), s.get('quote')): s for s in refs}.values())


def protected(record, field):
    mode = record.get('_provenance', {}).get(field, {}).get('mode')
    return mode == 'user' or (not mode and bool(record.get(field)))


def unique_text(*values):
    result = []
    for value in values:
        value = (value or '').strip()
        if value and value not in result and not any(value in prior for prior in result):
            result.append(value)
    return ' '.join(result)


class WorkAnalysis:
    def __init__(self, store):
        self.store = store

    def context_token(self, c, record, project=None):
        # Ignore display metadata and the analysis itself. Include protected
        # field provenance and linked project changes, not just record revision.
        work = {k: record.get(k) for k in FIELDS['tasks']}
        work['_provenance'] = record.get('_provenance', {})
        project = project or {}
        # A display ordering choice does not change work facts or reminders.
        context = {k: project.get(k) for k in FIELDS['projects'] if k != 'priority'}
        from incremental import enabled, provenance
        if enabled(c):
            # Project narrative is routed as a delta, not broadcast to every task.
            context = {}
            work['_provenance'] = provenance(work['_provenance'])
            work.pop('priority', None)
            work['_provenance'].pop('priority', None)
        from work_completion import WorkCompletion
        goal = WorkCompletion.goal(c, record['id'])
        if goal:
            work['_completion_goal'] = goal
        source_state = []
        raw_exists = c.execute("SELECT 1 FROM sqlite_master WHERE name='raw_heads'").fetchone()
        refs = source_refs(record) + [ref for item in (goal or {}).get('criteria', []) for ref in item['sources']]
        for source in refs:
            if source.get('type') == 'raw' and raw_exists:
                row = c.execute('''SELECT h.version_id,s.state,s.revision FROM raw_versions v
                  JOIN raw_sources s ON s.id=v.source_id
                  LEFT JOIN raw_heads h ON h.source_id=v.source_id AND h.object_id=v.object_id
                  WHERE v.id=?''', (source.get('id'),)).fetchone()
                state = tuple(row) if row else None
                if state and __import__('incremental').source_delta(c):
                    # Revisions are processed once as incoming evidence; retain
                    # source authority in the saved analysis, not the live head.
                    state = state[1:]
                source_state.append((source.get('id'), state))
        return digest({'work': work, 'project': context, 'source_state': sorted(source_state)})

    def _project(self, c, record):
        return self.store._records(c, 'projects').get(record.get('project_id'), {})

    def view(self, c, record, project=None, today=None):
        project = self._project(c, record) if project is None else project
        token = self.context_token(c, record, project)
        analysis = record.get('_work_analysis') or {}
        today = today or now()[:10]
        closed = record.get('state') in CLOSED or bool(record.get('archived'))
        completion_error = ''
        from work_completion import WorkCompletion
        completion = WorkCompletion(self.store)
        if analysis and not closed and completion.enabled(c) and completion.goal(c, record['id']):
            try:
                completion.validate(c, record, analysis.get('completion_check'), analysis=analysis)
            except StoreError as exc:
                completion_error = str(exc)
        changed = bool(analysis and (analysis.get('context_token') != token or completion_error))
        time_due = bool(analysis.get('review_on') and analysis['review_on'] < today)
        from incremental import enabled
        if enabled(c):
            time_due = False
        stale = changed or time_due
        decision = analysis.get('decision', {}).get('state', 'pending')
        current = unique_text(record.get('progress'), record.get('result')) or '当前情况尚未整理。'
        next_step = record.get('next_action') or '下一步尚未明确。'
        next_mode = 'recorded'
        alternative = ''
        if analysis and not changed and not protected(record, 'progress'):
            current = analysis['current']
        if closed:
            next_step = '工作已归档，后续安排保留在历史中。' if record.get('archived') else (
                '工作已完成，暂无待推进的下一步。' if record.get('state') == '已完成' else '工作已取消，暂无待推进的下一步。')
        elif analysis and not stale:
            if decision == 'pending':
                if protected(record, 'next_action') and record.get('next_action') != analysis['next_step']:
                    alternative = analysis['next_step']
                else:
                    next_step = analysis['next_step']
                    next_mode = 'suggestion' if analysis['timing_kind'] != 'agreed' else 'arrangement'
            elif decision == 'accepted':
                next_step = record.get('next_action') or analysis['next_step']
                next_mode = 'accepted'
            elif decision == 'dismissed' and not record.get('next_action'):
                next_step = '上次建议未采纳，下一步待重新安排。'
        return {'current': current, 'next_step': next_step, 'next_mode': next_mode,
                'has_analysis': bool(analysis), 'needs_review': not closed and (not analysis or stale),
                'stale': not closed and stale, 'reason': completion_error or ('工作或依据有更新' if changed else '已到复核时间' if time_due else '尚未分析' if not analysis else ''),
                'decision': decision if analysis else '', 'alternative': alternative,
                'can_decide': bool(analysis and not stale and not closed and decision == 'pending'),
                'context_token': token, 'review_on': '' if closed else analysis.get('review_on', ''),
                'analyzed_at': analysis.get('at', '')}

    def views(self, c, records, projects):
        projects = {p['id']: p for p in projects}
        return {r['id']: self.view(c, r, projects.get(r.get('project_id'), {})) for r in records}

    def impact_index(self, c):
        """All existing work is searchable context for new records, even if fresh.

        This is a navigation index, not a keyword-based completion classifier.
        Codex compares new evidence with the original goal and reads full details.
        Closed/archived work remains visible to prevent duplicate recreation.
        """
        projects = self.store._records(c, 'projects')
        people = self.store._records(c, 'people')
        result = []
        for work in self.store._records(c, 'tasks').values():
            result.append({
                'id': work['id'], 'revision': work['revision'],
                'title': work.get('title', ''), 'state': work.get('state', ''),
                'archived': bool(work.get('archived')),
                'project_id': work.get('project_id', ''),
                'project': projects.get(work.get('project_id'), {}).get('name', ''),
                'owner': people.get(work.get('owner_id'), {}).get('name', ''),
                'participants': [people.get(pid, {}).get('name', pid)
                                 for pid in work.get('participant_ids') or []],
                'progress': (work.get('progress') or '')[:300],
                'result': (work.get('result') or '')[:300],
                'next_action': (work.get('next_action') or '')[:300],
            })
        return sorted(result, key=lambda r: (r['archived'], r['state'] in CLOSED, r['id']))

    def packet(self, limit=20, ids=None):
        if type(limit) is not int or not 1 <= limit <= 200:
            raise StoreError('每批分析1至200项工作。')
        with self.store.connect() as c:
            c.execute('BEGIN')
            records = self.store._records(c, 'tasks')
            projects = self.store._records(c, 'projects')
            if ids and any(rid not in records for rid in ids):
                raise StoreError('工作不存在。', 404)
            meta = c.execute("SELECT value FROM wk_meta WHERE key='current_user_id'").fetchone()
            me = json.loads(meta[0]) if meta else ''
            result = []
            total = 0
            ordered = sorted(records.values(), key=lambda r: (
                not (r.get('owner_id') == me or me in (r.get('participant_ids') or [])), r['id']))
            for record in ordered:
                if record.get('archived') or record.get('state') in CLOSED:
                    continue
                project = projects.get(record.get('project_id'), {})
                view = self.view(c, record, project)
                total += int(view['needs_review'])
                if (ids and record['id'] not in ids) or (not ids and not view['needs_review']):
                    continue
                if len(result) < limit:
                    result.append({'id': record['id'], 'revision': record['revision'],
                                   'context_token': view['context_token'], 'reason': view['reason'],
                                   'work': record, 'project': project, 'sources': source_refs(record),
                                   'completion_goal': __import__('work_completion').WorkCompletion.goal(c, record['id'])})
            return {'pending': total, 'items': result,
                    'completion_instructions': __import__('work_completion').INSTRUCTIONS,
                    'instructions': '先按原工作目标核对是否已经完成、取消或仍需推进；结合相关人员、项目、聊天及会议文字交叉检查结果，不局限于原工作已挂的依据。每次新记录整理均核对相关工作的进展、结果、状态及安排，不以待复核队列为唯一范围。结合上下文已足以确定完成时先用work-append同步结果与状态并保存依据，不要求原文写出“已完成”，不当作待采纳建议；只补分析文字不算状态更新。不能把补纪要、汇总结论或独立后续事项追加为原工作的完成条件；有结果的工作不应继续要求用户核实是否发生。仅时间已过、到场、同名会议或中间结果不足以证明整体完成，人工决定继续保护。仍需推进时提交含时间的下一步；已完成时结束旧建议与复核。已有约定与建议分清，不凭空指定责任或期限；新建议使用稳定next_key，已否定建议不得换标识重开。'}

    @staticmethod
    def validate(c, values, closed=False):
        if not isinstance(values, dict) or set(values) != ANALYSIS_FIELDS:
            raise StoreError('工作分析需同时提供当前情况、下一步、时间依据、复核日期、原文与当前上下文版本。')
        clean = deepcopy(values)
        for key in ANALYSIS_FIELDS - {'sources'}:
            if not isinstance(clean[key], str) or len(clean[key]) > (12000 if key in ('current', 'next_step', 'rationale', 'timing_basis') else 250):
                raise StoreError('分析内容格式不正确或过长。')
            clean[key] = clean[key].strip()
        if any(not clean[key] for key in ('current', 'rationale', 'timing_basis', 'context_token')):
            raise StoreError('请说明当前情况及分析、时间安排的依据。')
        if not closed and (not clean['next_step'] or not clean['next_key'] or
                           (not clean['review_on'] and not __import__('incremental').enabled(c))):
            raise StoreError('进行中的工作应说明下一步、稳定行动标识和下次复核时间。')
        if clean['timing_kind'] not in ('agreed', 'suggested', 'mixed', 'none'):
            raise StoreError('时间须区分已有约定、建议或两者兼有。')
        if not closed and clean['timing_kind'] == 'none':
            raise StoreError('尚无约定时请提出建议行动或复核时间，不省略时间安排。')
        if clean['review_on']:
            try:
                dt.date.fromisoformat(clean['review_on'])
            except ValueError:
                raise StoreError('复核日期格式应为年-月-日。') from None
        from people import People
        clean['sources'] = People._refs(c, clean['sources'])
        return clean

    def save(self, rid, revision, values, actor='Codex工作分析', *, connection=None):
        if type(revision) is not int or revision < 0:
            raise StoreError('请先读取当前工作版本。')
        with (nullcontext(connection) if connection is not None else self.store.connect()) as c:
            if connection is None:
                c.execute('BEGIN IMMEDIATE')
            current = self.store._record(c, 'tasks', rid)
            if current.get('archived'):
                raise StoreError('已归档工作不自动重新分析。', 409)
            project = self._project(c, current)
            base = {k: v for k, v in values.items() if k != 'completion_check'} if isinstance(values, dict) else values
            clean = self.validate(c, base, current.get('state') in CLOSED)
            from work_completion import WorkCompletion
            completion = WorkCompletion(self.store)
            if completion.enabled(c):
                clean['completion_check'] = completion.validate(c, current, values.get('completion_check'), analysis=clean)
            old = current.get('_work_analysis') or {}
            token = self.context_token(c, current, project)
            # Same reviewed input is idempotent; user decisions remain intact.
            input_hash = digest({k: v for k, v in clean.items() if k != 'context_token'})
            if old.get('input_hash') == input_hash and old.get('context_token') == token:
                return {'id': rid, 'revision': current['revision'], 'unchanged': True,
                        'decision': old.get('decision', {}).get('state', 'pending')}
            if current['revision'] != revision or values['context_token'] != token:
                raise StoreError('工作、项目或原文已更新，请重新取分析上下文；旧分析未覆盖当前记录。', 409)
            row = c.execute("SELECT payload FROM wk_records WHERE kind='tasks' AND id=?", (rid,)).fetchone()
            before = json.loads(row[0]) if row else {}
            after = deepcopy(before)
            prior_decision = current.get('_work_analysis_decisions', {}).get(clean['next_key'])
            clean['decision'] = deepcopy(prior_decision or {'state': 'pending'})
            if prior_decision and prior_decision['state'] == 'accepted':
                clean['next_step'] = prior_decision['next_step']
            clean.update(at=now(), actor=actor, input_hash=input_hash)
            after['_work_analysis'] = clean
            effective = dict(current, **after)
            clean['context_token'] = self.context_token(c, effective, project)
            result = self.store._write(c, 'tasks', rid, after, before, revision,
                                       '整理工作分析', actor, '当前情况与下一步；事实、建议及人工决定分别保留')
            return dict(result, decision=clean['decision']['state'], view=self.view(c, dict(effective, revision=result['revision']), project))

    def decide(self, rid, revision, answer, actor='本机工作台'):
        if not isinstance(answer, dict) or set(answer) - {'action', 'next_step', 'note', 'context_token'}:
            raise StoreError('请提交采纳、修改后采纳或不采纳。')
        action = answer.get('action')
        if action not in ('accept', 'edit', 'dismiss'):
            raise StoreError('请提交采纳、修改后采纳或不采纳。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current = self.store._record(c, 'tasks', rid)
            if type(revision) is not int or current['revision'] != revision:
                raise StoreError('工作已更新，请保留输入并重新读取。', 409)
            project = self._project(c, current)
            view = self.view(c, current, project)
            original = current.get('_work_analysis')
            if not original or current.get('archived') or current.get('state') in CLOSED:
                raise StoreError('当前没有可以处理的推进建议。', 409)
            if original.get('decision', {}).get('state') != 'pending':
                raise StoreError('这项建议已经处理；可直接维护当前工作，不会自动重开。', 409)
            if action != 'dismiss' and (view['stale'] or answer.get('context_token') != view['context_token']):
                raise StoreError('分析依据或时间已有变化，请先重新核对，再采纳下一步。', 409)
            note = answer.get('note', '')
            if not isinstance(note, str) or len(note) > 2000:
                raise StoreError('处理说明格式不正确。')
            next_step = answer.get('next_step') if action == 'edit' else original['next_step']
            if action != 'dismiss' and (not isinstance(next_step, str) or not next_step.strip() or len(next_step) > 12000):
                raise StoreError('请填写含时间安排的下一步。')
            row = c.execute("SELECT payload FROM wk_records WHERE kind='tasks' AND id=?", (rid,)).fetchone()
            before = json.loads(row[0]); after = deepcopy(before)
            decision = {'state': 'dismissed' if action == 'dismiss' else 'accepted',
                        'at': now(), 'note': note.strip(), 'actor': actor,
                        'next_step': next_step.strip()}
            after.setdefault('_work_analysis_decisions', {})[original['next_key']] = decision
            analysis = after['_work_analysis']
            analysis['decision'] = decision
            if action != 'dismiss':
                after['next_action'] = decision['next_step']
                analysis['next_step'] = decision['next_step']
                after.setdefault('_provenance', {})['next_action'] = dict(mode='user', sources=analysis['sources'], at=now(), actor=actor)
            effective = dict(current, **after)
            analysis['context_token'] = self.context_token(c, effective, project)
            return self.store._write(c, 'tasks', rid, after, before, revision,
                                     {'accept': '采纳下一步', 'edit': '修改后采纳下一步', 'dismiss': '不采纳下一步'}[action], actor, note or '用户处理工作推进建议')
