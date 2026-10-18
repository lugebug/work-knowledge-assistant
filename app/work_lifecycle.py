"""Reviewed evidence advances/closes work, or creates a concrete user decision.

Codex determines the semantic relationship and sufficiency of evidence. This
service validates sources, persists that assessment, and commits its effects
atomically. It never classifies completion from a keyword or elapsed date.
"""
from copy import deepcopy
import hashlib
import json

from store import StoreError, json_text, now
from work_model import Work
from work_analysis import WorkAnalysis, CLOSED, unique_text
from judgment_cards import JudgmentCards

CHANGE_FIELDS = {'progress', 'result', 'next_action', 'state', 'due_date', 'schedule_note'}
LABELS = {'advance': '推进工作', 'close': '关闭工作', 'decide': '待你判断', 'unchanged': '无工作变化'}


class WorkLifecycle:
    def __init__(self, store):
        self.store = store

    def initialize(self):
        with self.store.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS wk_work_evidence_reviews(
              version_id TEXT PRIMARY KEY, input_hash TEXT NOT NULL,
              payload TEXT NOT NULL, result TEXT NOT NULL, reviewed_at TEXT NOT NULL)''')

    def enable(self):
        self.initialize()
        with self.store.connect() as c:
            c.execute("INSERT OR REPLACE INTO wk_meta VALUES('work_lifecycle_version','35')")
        return {'enabled': True, 'version': 35}

    @staticmethod
    def enabled(c):
        row = c.execute("SELECT value FROM wk_meta WHERE key='work_lifecycle_version'").fetchone()
        return bool(row and json.loads(row[0]) >= 35)

    @staticmethod
    def exists(c):
        return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='wk_work_evidence_reviews'").fetchone())

    def get(self, c, version_id):
        if not self.exists(c):
            return None
        row = c.execute('SELECT * FROM wk_work_evidence_reviews WHERE version_id=?', (version_id,)).fetchone()
        return dict(json.loads(row['result']), reviewed_at=row['reviewed_at'],
                    assessment=json.loads(row['payload'])) if row else None

    def history(self, c, work_id):
        if not self.exists(c):
            return []
        result = []
        for row in c.execute('SELECT * FROM wk_work_evidence_reviews ORDER BY reviewed_at DESC,version_id'):
            outcomes = [x for x in json.loads(row['result'])['outcomes'] if x['work_id'] == work_id]
            if outcomes:
                payload = json.loads(row['payload'])
                result.append(dict(version_id=row['version_id'], reviewed_at=row['reviewed_at'],
                                   summary=payload['summary'], sources=payload['sources'], outcomes=outcomes))
        return result

    @staticmethod
    def _text(value, name, maximum=6000):
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise StoreError('请填写' + name + '，并控制在合理长度内。')
        return value.strip()

    def _changes(self, c, values, allow_empty=True):
        if not isinstance(values, dict) or set(values) - CHANGE_FIELDS or (not values and not allow_empty):
            raise StoreError('请列出要同步的进展、结果、状态、下一步或时间。')
        return self.store._validate(c, 'tasks', values) if values else {}

    @staticmethod
    def _question_id(work_id, proposal, key=''):
        # The same completion decision cannot be reopened by a new source/date.
        intent = ('close:' + proposal['state']) if proposal.get('state') in CLOSED else key or json_text(proposal)
        return 'work-lifecycle-' + hashlib.sha256((work_id + ':' + intent).encode()).hexdigest()[:24]

    def _question(self, c, record, proposal, reason, unknowns, summary, sources, version_id, key=''):
        qid = self._question_id(record['id'], proposal, key)
        prior = c.execute('SELECT payload FROM wk_judgment_cards WHERE id=?', (qid,)).fetchone()
        receipts = list(dict.fromkeys([*(json.loads(prior[0]).get('receipt_ids', []) if prior else []), version_id]))
        closing = proposal.get('state') in CLOSED
        old_values = {k: record.get(k) or '' for k in proposal}
        card = {
            'id': qid, 'category': 'ambiguity',
            'question': ('是否关闭' if closing else '如何更新') + '「' + record['title'] + '」？',
            'background': '当前状态：' + record['state'] + '。' + unique_text(record.get('progress'), record.get('result')) + '\n新证据：' + summary,
            'impact': '确认后更新这项工作的结果及状态，并停止本项推进。' if closing else '确认后将下方具体内容写回原工作，已有过程保留。',
            'recommendation': '建议按“采用以下更新”中的内容处理；若不符合实际，可修改内容或保留当前记录。',
            'rationale': reason, 'unknowns': unknowns,
            'checked': '已核对本项工作当前记录及所列原文；能确定的部分已先保存。' + reason,
            'sources': sources, 'receipt_ids': receipts, 'recommended_option': 'apply',
            'options': [
                {'id': 'apply', 'label': '采用以下更新', 'description': '按所列内容更新原工作。',
                 'operations': [{'kind': 'tasks', 'id': record['id'], 'values': proposal}]},
                {'id': 'keep', 'label': '保留当前记录', 'description': '本次不采用建议中的变更。',
                 'operations': [{'kind': 'tasks', 'id': record['id'], 'values': old_values}]},
            ],
            'editable': [{'operation': 0, 'field': field, 'label': {
                'progress': '当前情况', 'result': '已有结果', 'next_action': '下一步（含时间）',
                'due_date': '约定截止日期', 'schedule_note': '时间说明'}[field], 'editor': 'textarea'}
                for field in proposal if field != 'state'][:4],
        }
        return JudgmentCards(self.store).propose(card, connection=c)

    def _resolve_question(self, c, qid, version_id):
        row = c.execute('SELECT * FROM wk_judgment_cards WHERE id=?', (qid,)).fetchone()
        if not row or row['state'] not in ('pending', 'deferred'):
            return
        before = dict(row)
        answer = '较新证据已足以确定，已通过工作推进／关闭入口写回；依据版本：' + version_id
        c.execute("UPDATE wk_judgment_cards SET state='resolved',answer=?,revision=revision+1,updated_at=? WHERE id=?",
                  (answer, now(), qid))
        after = dict(c.execute('SELECT * FROM wk_judgment_cards WHERE id=?', (qid,)).fetchone())
        JudgmentCards._event(c, qid, '已按新证据更新工作', before, after, 'Codex依据整理')
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='wk_review_resolutions'").fetchone():
            pending = [json.loads(x[0]).get('receipt_ids', []) for x in c.execute("SELECT payload FROM wk_judgment_cards WHERE state IN ('pending','deferred')")]
            for vid in json.loads(row['payload']).get('receipt_ids', []):
                if not any(vid in ids for ids in pending) and c.execute('SELECT 1 FROM wk_review_receipts WHERE version_id=?', (vid,)).fetchone():
                    c.execute('INSERT OR IGNORE INTO wk_review_resolutions VALUES(?,?,?)', (vid, answer, now()))

    def review(self, document):
        if not isinstance(document, dict) or set(document) != {'version_id', 'summary', 'sources', 'related'}:
            raise StoreError('请提供本条证据、综合判断、原文摘录及相关工作逐项处理。')
        vid = self._text(document['version_id'], '证据版本', 160)
        summary = self._text(document['summary'], '综合判断')
        document = deepcopy(document)
        related = document['related']
        if not isinstance(related, list) or len(related) > 100:
            raise StoreError('相关工作应为列表；无关联时留空并说明理由。')
        input_hash = hashlib.sha256(json_text(document).encode()).hexdigest()
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            source = c.execute('''SELECT v.payload,s.state FROM raw_versions v JOIN raw_sources s ON s.id=v.source_id WHERE v.id=?''', (vid,)).fetchone()
            if not source or source['state'] != 'approved':
                raise StoreError('证据版本不存在或不在已批准来源内。', 403)
            old = c.execute('SELECT input_hash,result FROM wk_work_evidence_reviews WHERE version_id=?', (vid,)).fetchone()
            if old:
                if old['input_hash'] != input_hash:
                    raise StoreError('本证据已有工作处理判断，不能覆盖或重放不同结论；请读取原结果，新证据另行处理，人工纠正使用原工作维护入口。', 409)
                return dict(json.loads(old['result']), unchanged=True)
            from people import People
            refs = People._refs(c, document['sources'])
            if not any(r['type'] == 'raw' and r['id'] == vid for r in refs):
                raise StoreError('依据必须包含本次证据版本的准确原句。')
            from task_work_links import TaskWorkLinks
            link_questions = TaskWorkLinks(self.store).require_review(c, vid, related, refs)
            outcomes, seen = [], set()
            for item in related:
                allowed = {'work_id', 'revision', 'action', 'reason', 'changes', 'decision', 'analysis', 'date', 'decision_key', 'completion_check'}
                if not isinstance(item, dict) or {'work_id', 'revision', 'action', 'reason', 'changes'} - set(item) or set(item) - allowed:
                    raise StoreError('每项关联需说明工作、当前版本、推进／关闭／待判断／无变化、理由及具体改动。')
                rid, action = item['work_id'], item['action']
                if not isinstance(rid, str) or rid in seen or action not in LABELS:
                    raise StoreError('相关工作重复或处理方式不正确。')
                seen.add(rid)
                reason = self._text(item['reason'], '本项工作判断依据')
                record = self.store._record(c, 'tasks', rid)
                if type(item['revision']) is not int or record['revision'] != item['revision']:
                    raise StoreError('相关工作已变化，请重新核对后处理；整批未写入。', 409)
                changes = self._changes(c, item['changes'], action in ('decide', 'unchanged'))
                from work_completion import WorkCompletion
                completion = WorkCompletion(self.store)
                if completion.enabled(c):
                    item['completion_check'] = completion.validate(c, record, item.get('completion_check'),
                        action=action, changes=changes, analysis=item.get('analysis'))
                if action == 'unchanged':
                    if changes or item.get('decision') or item.get('analysis'):
                        raise StoreError('无变化判断不能同时提交工作变更。')
                    outcomes.append(dict(work_id=rid, action=action, outcome='unchanged', reason=reason, change_ids=[], state=record['state'], revision=record['revision']))
                    continue
                if record.get('archived'):
                    raise StoreError('已归档工作不自动更新；请核对关联范围，需恢复时由用户处理。', 409)
                if action in ('advance', 'close') and record.get('state') in CLOSED:
                    raise StoreError('已结束工作不自动重开或重新关闭；无新变化请登记无变化，需要改变决定时提出待判断。', 409)
                if action == 'close':
                    changes.setdefault('state', '已完成')
                    if changes['state'] not in CLOSED or not changes.get('result', '').strip():
                        raise StoreError('关闭工作须说明实际结果，并选择已完成或已取消。')
                elif changes.get('state') in CLOSED:
                    raise StoreError('自动关闭请使用关闭工作；不确定的关闭内容放入待判断建议，不能先写为完成。')
                decision = item.get('decision')
                if action == 'decide':
                    if not isinstance(decision, dict) or set(decision) != {'unknowns', 'proposal'}:
                        raise StoreError('待判断需说明尚不能确定的部分及确认后实际改动。')
                    unknowns = self._text(decision['unknowns'], '待用户判断的具体不确定内容')
                    proposal = self._changes(c, decision['proposal'], False)
                else:
                    if decision:
                        raise StoreError('确定的更新与待判断处理请明确选择。')
                    unknowns, proposal = '', {}
                if 'state' in proposal and proposal['state'] in CLOSED and not proposal.get('result'):
                    raise StoreError('建议关闭也须说明确认后的完成或取消结果。')
                change_ids = []
                key = item.get('decision_key', '')
                if not isinstance(key, str) or len(key) > 250:
                    raise StoreError('判断标识无效。')
                qid = self._question_id(rid, proposal or changes, key)
                previous_question = c.execute('SELECT state FROM wk_judgment_cards WHERE id=?', (qid,)).fetchone()
                if previous_question and previous_question['state'] in ('resolved', 'dismissed'):
                    outcomes.append(dict(work_id=rid, action=action, outcome='protected_decision', reason=reason,
                                         judgment_id=qid, change_ids=[], state=record['state'], revision=record['revision']))
                    continue
                if changes:
                    # Completion state and its result must stay together when
                    # either conflicts with an existing human-maintained value.
                    if action == 'close':
                        prior = c.execute("SELECT payload FROM wk_records WHERE kind='tasks' AND id=?", (rid,)).fetchone()
                        overlay = json.loads(prior[0]) if prior else {}
                        provenance = overlay.get('_provenance') or {}
                        closure_conflict = any(
                            record.get(k) != changes[k] and
                            (provenance.get(k, {}).get('mode') == 'user' or
                             (k in overlay and not provenance.get(k) and overlay[k] not in ('', None, [])))
                            for k in ('state', 'result'))
                        if closure_conflict:
                            proposal = dict(proposal, state=changes.pop('state'), result=changes.pop('result'))
                            unknowns = unique_text(unknowns, '关闭结果与已有人工维护不同，需你决定是否采用；原状态保持。')
                    saved = Work(self.store).append(rid, record['revision'], {
                        'key': 'evidence-' + hashlib.sha256((vid + ':' + rid).encode()).hexdigest()[:24],
                        'text': LABELS[action] + '：' + summary + '\n判断依据：' + reason,
                        'mode': 'fact', 'sources': refs, 'date': item.get('date', ''), 'changes': changes,
                    }, 'Codex工作推进', connection=c)
                    change_ids.append(saved['change_id'])
                    blocked = {k: changes[k] for k in saved['protected']}
                    if blocked:
                        proposal = dict(blocked, **proposal)
                        if changes.get('state') in CLOSED:
                            proposal.setdefault('result', changes['result'])
                        unknowns = unique_text(unknowns, '拟更新内容与已有人工维护不同，需你决定是否采用新证据对应的更新。')
                    record = self.store._record(c, 'tasks', rid)
                question = None
                if proposal:
                    question = self._question(c, record, proposal, reason, unknowns, summary, refs, vid, key)
                service = WorkAnalysis(self.store)
                if record['state'] in CLOSED and not question:
                    analysis = dict(current=unique_text(record.get('progress'), record.get('result')),
                        next_step='工作已结束，后续事项在各自工作继续。', next_key='closed-' + rid,
                        timing_kind='none', timing_basis='原工作已按证据结束，不再生成本项推进或复核日期。',
                        review_on='', rationale=reason, sources=refs,
                        context_token=service.context_token(c, record, service._project(c, record)))
                    if completion.enabled(c):
                        analysis['completion_check'] = deepcopy(item['completion_check'])
                        analysis['completion_check']['timing'] = [x for x in analysis['completion_check']['timing']
                            if x['field'] not in ('analysis_next_step', 'review_on')]
                    saved = service.save(rid, record['revision'], analysis, connection=c)
                    change_ids.append(saved['change_id'])
                elif item.get('analysis'):
                    analysis = deepcopy(item['analysis'])
                    if not isinstance(analysis, dict) or 'context_token' in analysis:
                        raise StoreError('同步分析使用标准分析内容；当前版本由本次原子更新生成。')
                    analysis['context_token'] = service.context_token(c, record, service._project(c, record))
                    if completion.enabled(c):
                        analysis['completion_check'] = item['completion_check']
                    saved = service.save(rid, record['revision'], analysis, connection=c)
                    if saved.get('change_id'):
                        change_ids.append(saved['change_id'])
                record = self.store._record(c, 'tasks', rid)
                if not question:
                    self._resolve_question(c, qid, vid)
                outcomes.append(dict(work_id=rid, action=action,
                    outcome=('protected_decision' if question.get('protected') else 'needs_decision') if question else 'applied',
                    reason=reason, change_ids=change_ids, judgment_id=question['id'] if question else '',
                    state=record['state'], revision=record['revision'], needs_analysis=service.view(c, record)['needs_review']))
            for outcome in outcomes:
                item = next(x for x in related if x['work_id'] == outcome['work_id'])
                if item.get('completion_check'):
                    outcome['completion_check'] = item['completion_check']
            result = {'version_id': vid, 'outcomes': outcomes,
                      'change_ids': [x for item in outcomes for x in item['change_ids']],
                      'judgment_ids': list(dict.fromkeys([*link_questions, *[item['judgment_id'] for item in outcomes if item['outcome'] == 'needs_decision']]))}
            payload = dict(deepcopy(document), sources=refs)
            c.execute('INSERT INTO wk_work_evidence_reviews VALUES(?,?,?,?,?)',
                      (vid, input_hash, json_text(payload), json_text(result), now()))
            return result

    def receipt_check(self, c, version_id):
        review = self.get(c, version_id)
        if not review:
            raise StoreError('本条新证据尚未完成相关工作的推进／关闭判断；请先关联工作并保存处理，无关联也须说明。', 409)
        pending = []
        for qid in review.get('judgment_ids', []):
            row=c.execute('SELECT state FROM wk_judgment_cards WHERE id=?',(qid,)).fetchone()
            if row and row[0] in ('pending','deferred'): pending.append(qid)
        for item in review['outcomes']:
            if item.get('judgment_id'):
                row = c.execute('SELECT state FROM wk_judgment_cards WHERE id=?', (item['judgment_id'],)).fetchone()
                if row and row['state'] in ('pending', 'deferred'):
                    pending.append(item['judgment_id'])
            if item['change_ids'] or item.get('completion_check'):
                record = self.store._record(c, 'tasks', item['work_id'])
                if WorkAnalysis(self.store).view(c, record)['needs_review']:
                    raise StoreError('工作已推进，请先保存最新两段分析，再登记本证据处理完成：' + record['title'], 409)
        return review, list(dict.fromkeys(pending))
