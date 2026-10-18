"""Critical business dimensions and their shared, evidence-aware review workflow.

Missing facts are always visible. Deferring an answer never counts as complete;
only a real value or an explicit, reasoned not-applicable decision closes a gap.
"""
import hashlib
import json
import re
from model import KINDS, PROJECT_TYPES, ORG_ROLES, DEPARTMENT_TYPES
from store import StoreError, json_text, now
from people import People

KIND_LABELS = dict(projects='项目', people='人员', contacts='聊天账号', organizations='组织',
                   departments='部门', capabilities='产品能力', roadmap='产品路线',
                   demands='需求', tasks='待办', updates='工作进展')
ORG_LINKS = dict(customer_ids='客户', channel_ids='渠道', partner_ids='合作伙伴',
                 supplier_ids='供应方', implementation_org_ids='实施交付方',
                 operating_org_ids='运营方', internal_org_ids='内部参与组织')
EMPTY = {'', '-', '—', '未知', '不详', '待补', '待补充', '未填写', '待明确', '待指定',
         '暂无明确资料', '未关联', '未设置', '尚未确认', '尚未填写', '组织待补充', '未提供'}


def known(value):
    if isinstance(value, (list, dict)): return bool(value)
    return isinstance(value, str) and value.strip() not in EMPTY and not re.fullmatch(
        r'(?:资料|职责|负责人|单位|组织|时间)?(?:待补充|待核对|待确认|暂不清楚|尚未明确|尚未记录)[。.]?', value.strip())


def dim(key, label, field=None, editor='text', target=None, alternatives=(), hint='', options=(), required=True):
    return dict(key=key, label=label, field=field or key, editor=editor, target=target,
                alternatives=list(alternatives), hint=hint, options=list(options), required=required)


RULES = {
    'projects': [
        dim('name', '项目名称'),
        dim('primary_type', '主要工作类型', editor='choice', options=PROJECT_TYPES),
        dim('goal', '项目目标与需求', alternatives=('request_summary',), hint='客户或内部要解决什么问题。'),
        dim('boundary', '工作范围与边界', hint='本次做什么，哪些内容不在本次范围内。'),
        dim('expected_output', '预期交付或结果', hint='例如方案、测试结论、部署或运营效果。'),
        dim('organizations', '参与组织及合作角色', editor='organizations', hint='至少明确一个参与组织；组织可以承担不同角色。'),
        dim('owner_id', '项目负责人', editor='relation', target='people', alternatives=('owner',), hint='对项目整体负责的人；不能把所有群成员当成负责人。'),
        dim('stage', '当前阶段与进展', alternatives=('progress',)),
        dim('next_action', '下一步安排', hint='只填已经确定的安排；尚未决定可暂不知道。'),
        dim('business_value', '业务价值', hint='为什么要做，例如收入、客户关系、复用能力或内部效率。'),
        dim('schedule', '时间安排', field='calendar_estimate', alternatives=('target_date', 'milestones'), hint='可保留“下周交付”等原文；未定时间不要编造日期。'),
    ],
    'people': [
        dim('name', '姓名或常用称呼'),
        dim('organization_id', '所属组织', editor='relation', target='organizations'),
        dim('work_scope', '职务或工作职责', alternatives=('role',), hint='实际做什么；明确的项目分工也可以作为职责依据。'),
        dim('department', '所属部门', editor='relation', target='departments', required='internal'),
        dim('manager_id', '直属汇报对象', editor='relation', target='people', required='internal', hint='仅内部同事需明确；没有直属上级时可说明原因并标记不适用。'),
        dim('project_ids', '参与项目', editor='relations', target='projects', hint='没有具体项目可明确标记不适用，不会把空白解释成没有项目。'),
    ],
    'contacts': [dim('person_id', '账号对应人员', editor='relation', target='people', hint='昵称相似不能直接认定；已有身份线索也会保留在本页。')],
    'organizations': [
        dim('name', '组织名称'),
        dim('roles', '与我们的业务关系', editor='choices', options=ORG_ROLES),
        dim('description', '合作内容或组织职责'),
        dim('owner_id', '单位负责人', editor='relation', target='people', hint='这是单位负责人，普通工作联系人不会自动填入。'),
    ],
    'departments': [
        dim('name', '部门或分组名称'),
        dim('organization_id', '所属组织', editor='relation', target='organizations'),
        dim('unit_type', '部门或分组类型', editor='choice', options=DEPARTMENT_TYPES),
        dim('owner_id', '负责人', editor='relation', target='people'),
        dim('description', '职责范围'),
    ],
    'capabilities': [dim('state', '能力核对状态', editor='choice', options=['资料支持，待核对', '特定项目已验证', '需修订资料', '已停用'])],
    'roadmap': [dim('name', '方向名称'), dim('description', '目标与内容'),
        dim('owner_id', '负责人', editor='relation', target='people', alternatives=('owner',)),
        dim('priority', '优先级', editor='choice', options=['高', '中', '低']),
        dim('target_date', '目标日期', editor='date')],
    'demands': [dim('title', '需求内容'), dim('project_id', '关联项目', editor='relation', target='projects'),
        dim('requester_id', '需求提出人', editor='relation', target='people'),
        dim('owner_id', '承接负责人', editor='relation', target='people', alternatives=('owner',)),
        dim('stage', '当前处理进展'), dim('priority', '优先级', editor='choice', options=['高', '中', '低']),
        dim('target_date', '期望时间', editor='date')],
    'tasks': [dim('title', '要做的事'), dim('project_id', '关联项目', editor='relation', target='projects', hint='跨项目或日常工作可说明原因并标记不适用。'),
        dim('owner_id', '承接负责人', editor='relation', target='people', alternatives=('owner',)),
        dim('schedule', '时间安排', field='schedule_note', alternatives=('due_date',), hint='可以填写具体日期或已约定的相对时间。')],
    'updates': [dim('title', '进展主题'), dim('description', '实际进展'),
        dim('project_id', '关联项目', editor='relation', target='projects', hint='跨项目汇报可明确标记不适用。'),
        dim('source_date', '进展涉及日期', editor='date'),
        dim('participant_ids', '相关人员', editor='relations', target='people', alternatives=('owner_id', 'participating_department_ids'), hint='汇报或参与人员，不等于个人唯一负责。')],
}


class Completeness:
    def __init__(self, store):
        self.store = store
        self.people = People(store)

    def initialize(self):
        with self.store.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS wk_completeness_answers(
                kind TEXT NOT NULL,record_id TEXT NOT NULL,dimension TEXT NOT NULL,
                state TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',revision INTEGER NOT NULL,
                updated_at TEXT NOT NULL,PRIMARY KEY(kind,record_id,dimension));
            CREATE TABLE IF NOT EXISTS wk_completeness_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT NOT NULL,record_id TEXT NOT NULL,
                dimension TEXT NOT NULL,action TEXT NOT NULL,before_json TEXT NOT NULL,
                after_json TEXT NOT NULL,actor TEXT NOT NULL,happened_at TEXT NOT NULL);
            ''')

    def _answers(self, c):
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='wk_completeness_answers'").fetchone(): return {}
        return {(r['kind'], r['record_id'], r['dimension']): dict(r) for r in c.execute('SELECT * FROM wk_completeness_answers')}

    def _resolved(self, kind, r, spec, records, relations):
        key = spec['key']
        active = lambda k, rid: rid in records[k] and not records[k][rid].get('archived')
        rel = [v for v in relations if v['state'] in ('supported', 'confirmed') and v['person_id'] == r['id']]
        if kind == 'people':
            if key == 'organization_id':
                return active('organizations', r.get(key)) or any(v['kind'] == 'organizations' and active('organizations', v['target_id']) for v in rel)
            if key == 'department':
                return any(v['kind'] == 'departments' and active('departments', v['target_id']) for v in rel)
            if key == 'project_ids':
                return any(active('projects', pid) for pid in r.get(key, [])) or any(v['kind'] == 'projects' and active('projects', v['target_id']) for v in rel) or any(not p.get('archived') and (p.get('owner_id') == r['id'] or r['id'] in p.get('participant_ids', [])) for p in records['projects'].values())
            if key == 'work_scope' and any(v['kind'] == 'projects' and v.get('label') not in ('工作群沟通参与者', '资料明确的协作参与') for v in rel): return True
        if kind == 'projects' and key == 'organizations':
            return any(any(active('organizations', rid) for rid in r.get(field, [])) for field in ORG_LINKS)
        value = r.get(spec['field'])
        if spec['editor'] == 'relation':
            return active(spec['target'], value) or any(known(r.get(f)) for f in spec['alternatives'])
        if spec['editor'] == 'relations':
            return any(active(spec['target'], rid) for rid in value or []) or any(known(r.get(f)) for f in spec['alternatives'])
        return known(value) or any(known(r.get(f)) for f in spec['alternatives'])

    def _sources(self, c, r, rel):
        refs = list(r.get('_dossier_sources') or [])
        refs += [s for p in r.get('_provenance', {}).values() for s in p.get('sources', [])]
        refs += [s for v in rel for s in v.get('sources', [])]
        if not refs and r.get('evidence_ids'):
            refs += [dict(type='message' if e.get('message_id') else 'document', id=e.get('message_id') or e.get('document_id'), quote=e.get('quote', ''), title=e.get('conversation_name') or e.get('title') or '工作依据') for e in self.store._evidence(c, r['evidence_ids'])]
        unique = {}
        for ref in refs:
            if ref.get('quote'): unique.setdefault((ref.get('type'), ref.get('id'), ref.get('quote')), ref)
        return list(unique.values())[:8]

    def _suggestions(self, kind, record, spec, records, relations):
        # These are already associated people, not inferred managers or employers.
        if spec.get('target') != 'people' or spec['key'] != 'owner_id': return []
        ids = list(record.get('participant_ids') or [])
        ids += [r['person_id'] for r in relations if r['kind'] == kind and r['target_id'] == record['id'] and r['state'] in ('supported', 'confirmed')]
        if kind == 'organizations': ids += [p['id'] for p in records['people'].values() if p.get('organization_id') == record['id']]
        result = []
        for pid in dict.fromkeys(ids):
            p = records['people'].get(pid)
            if p and not p.get('archived'):
                result.append(dict(id=pid, name=p['name'], reason='已有相关工作关系，负责人仍需指定'))
        return result[:8]

    def scan(self, c):
        records = {k: self.store._records(c, k) for k in KINDS}
        relations = self.people.relations(c)
        answers = self._answers(c)
        rows = []
        summary = dict(entities=0, incomplete_entities=0, missing_dimensions=0, deferred_dimensions=0,
                       not_applicable_dimensions=0, complete_entities=0, by_kind={})
        for kind in KINDS:
            count = dict(entities=0, incomplete_entities=0, missing_dimensions=0)
            for rid, r in records[kind].items():
                if r.get('archived') or (kind == 'contacts' and r.get('state') == '非工作联系人'): continue
                dims = []
                applicable = [s for s in RULES[kind] if s['required'] is True or s['required'] == 'internal' and r.get('state') == '内部同事']
                unresolved = 0
                for spec in applicable:
                    answer = answers.get((kind, rid, spec['key']), {})
                    filled = self._resolved(kind, r, spec, records, relations)
                    state = 'filled' if filled else answer.get('state', 'missing')
                    if state not in ('filled', 'not_applicable', 'deferred'): state = 'missing'
                    if state in ('missing', 'deferred'): unresolved += 1
                    if state != 'filled' or answer:
                        entry = dict(spec, state=state, note=answer.get('note', ''),
                                     value=r.get(spec['field'], ''), suggestions=self._suggestions(kind, r, spec, records, relations))
                        dims.append(entry)
                    if state == 'deferred': summary['deferred_dimensions'] += 1
                    if state == 'not_applicable': summary['not_applicable_dimensions'] += 1
                summary['entities'] += 1; count['entities'] += 1
                if unresolved:
                    summary['incomplete_entities'] += 1; count['incomplete_entities'] += 1
                    summary['missing_dimensions'] += unresolved; count['missing_dimensions'] += unresolved
                else: summary['complete_entities'] += 1
                if not unresolved and not any((kind, rid, s['key']) in answers for s in applicable): continue
                related = [v for v in relations if v['state'] in ('supported', 'confirmed') and (kind == 'people' and v['person_id'] == rid or v['kind'] == kind and v['target_id'] == rid)]
                context = [r.get(k) for k in ('role', 'work_scope', 'description', 'stage', 'progress', 'next_action', 'directory_department') if known(r.get(k))]
                context += [v.get('label', '') + '：' + (v['target_name'] if kind == 'people' else v['person_name']) for v in related[:6]]
                rows.append(dict(key='completeness:' + kind + ':' + rid, kind='completeness', id=rid,
                    target_kind=kind, title=r.get('name') or r.get('title') or rid, revision=r['revision'],
                    state='pending' if unresolved else 'completed', dimensions=dims,
                    missing_count=unresolved, total_count=len(applicable), complete_count=len(applicable)-unresolved,
                    description='、'.join(s['label'] for s in dims if s['state'] in ('missing', 'deferred')),
                    detail='；'.join(dict.fromkeys(str(x) for x in context))[:1800],
                    basis='已对照现有资料与明确关系，以下关键维度仍缺少可直接记录的答案。',
                    sources=self._sources(c, r, related), note='', can_confirm=False))
            summary['by_kind'][kind] = count
        return dict(items=rows, summary=summary, rules={k:[dict(s) for s in v] for k,v in RULES.items()})

    def _event(self, c, kind, rid, key, action, before, after):
        c.execute('INSERT INTO wk_completeness_events(kind,record_id,dimension,action,before_json,after_json,actor,happened_at) VALUES(?,?,?,?,?,?,?,?)',
                  (kind, rid, key, action, json_text(before), json_text(after), '用户', now()))

    def apply(self, c, row, requested):
        if not isinstance(requested, dict) or not requested: raise StoreError('请填写答案，或明确选择暂不知道／不适用。')
        specs = {s['key']:s for s in row['dimensions']}
        if set(requested) - set(specs): raise StoreError('关键资料项已变化，请重新读取。', 409)
        kind, rid = row['target_kind'], row['id']
        record = self.store._record(c, kind, rid)
        for key, answer in requested.items():
            spec = specs[key]
            if not isinstance(answer, dict) or set(answer) - {'action', 'value', 'note'}: raise StoreError('答案格式不正确。')
            action = answer.get('action')
            if action not in ('set', 'defer', 'not_applicable', 'reopen'): raise StoreError('请选择填写、暂不知道或不适用。')
            note = answer.get('note', '').strip() if isinstance(answer.get('note', ''), str) else None
            if note is None or len(note) > 2000: raise StoreError('说明最多2000字。')
            if action == 'not_applicable' and not note: raise StoreError('标记不适用时，请说明原因。')
            if spec['state'] == 'filled' and action in ('defer', 'not_applicable', 'reopen'):
                raise StoreError('这项已有实际内容，请到档案维护内容，不能用状态掩盖已知事实。')
            prior = c.execute('SELECT * FROM wk_completeness_answers WHERE kind=? AND record_id=? AND dimension=?', (kind,rid,key)).fetchone()
            before = dict(prior) if prior else {}
            if action == 'set':
                value = answer.get('value')
                if not known(value): raise StoreError(spec['label']+'需要实际答案，不能用待补充等占位文字。')
                values = {}
                if spec['editor'] == 'organizations':
                    if not isinstance(value, dict) or set(value) - set(ORG_LINKS) or not any(value.values()): raise StoreError('请选择参与组织及合作角色。')
                    values.update(value)
                elif key == 'department' and kind == 'people':
                    department = self.store._record(c, 'departments', value)
                    if department.get('archived'): raise StoreError('不能关联已归档部门。')
                    self.people.create_relation_in_transaction(c, dict(person_id=rid, kind='departments', target_id=value,
                        label='所属部门（用户确认）', detail=note, represented_org_id=department['organization_id']))
                else:
                    if spec['editor'] == 'choice' and value not in spec['options']: raise StoreError('请选择有效选项。')
                    if spec['editor'] == 'choices' and (not isinstance(value, list) or any(v not in spec['options'] for v in value)): raise StoreError('请选择有效选项。')
                    if spec.get('target'):
                        for target in value if spec['editor'] == 'relations' else [value]:
                            if self.store._record(c, spec['target'], target).get('archived'): raise StoreError('不能关联已归档档案。')
                    values[spec['field']] = value
                    if kind == 'contacts' and key == 'person_id':
                        values.update(candidate_person_id='', mapping_note='用户在待你判断中明确对应人员。')
                if values:
                    current = self.store._record(c, kind, rid)
                    self.store.update_in_transaction(c, kind, rid, values, current['revision'], '用户',
                        '待你判断补齐：'+spec['label']+('；'+note if note else ''))
                state = 'answered'
            else: state = {'defer':'deferred', 'not_applicable':'not_applicable', 'reopen':'missing'}[action]
            rev = before.get('revision', 0) + 1
            after = dict(kind=kind, record_id=rid, dimension=key, state=state, note=note, revision=rev, updated_at=now())
            c.execute('INSERT INTO wk_completeness_answers VALUES(?,?,?,?,?,?,?) ON CONFLICT(kind,record_id,dimension) DO UPDATE SET state=excluded.state,note=excluded.note,revision=excluded.revision,updated_at=excluded.updated_at',
                (kind, rid, key, state, note, rev, after['updated_at']))
            self._event(c, kind, rid, key, action, before, after)
        return dict(key=row['key'], answered=len(requested))


def protected_dimension_fields(c, kind, rid):
    """A user-marked not-applicable dimension must not be silently re-filled."""
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='wk_completeness_answers'").fetchone(): return set()
    protected = {r[0] for r in c.execute("SELECT dimension FROM wk_completeness_answers WHERE kind=? AND record_id=? AND state='not_applicable'", (kind,rid))}
    fields = set()
    for spec in RULES.get(kind, []):
        if spec['key'] in protected:
            fields.update(ORG_LINKS if spec['editor'] == 'organizations' else [spec['field'], *spec['alternatives']])
    return fields
