"""Work records own their history; people own their platform accounts.

The contacts adapter only preserves old source references. It is not a second
business editor. All writes use Store's revision checks and audit trail.
"""
from copy import deepcopy
from contextlib import nullcontext
import datetime as dt
import hashlib
import json
import uuid

from store import StoreError, now, json_text

VERSION_KEY = 'work_model_version'
PRIORITIES = ['', '高', '普通', '低']
ACCOUNT_EDITABLE = ('name', 'nickname', 'remark', 'account_alias', 'notes')
ACCOUNT_FIELDS = ('id', 'platform', 'account_id', 'source_scope', *ACCOUNT_EDITABLE,
                  'sources', '_user_fields', 'mapping_note', 'archived')


def enabled(c):
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='wk_meta'").fetchone():
        return False
    row = c.execute('SELECT value FROM wk_meta WHERE key=?', (VERSION_KEY,)).fetchone()
    return bool(row and json.loads(row[0]) >= 28)


def references(record):
    refs = list(record.get('_dossier_sources') or [])
    for entry in record.get('_provenance', {}).values():
        refs.extend(entry.get('sources') or [])
    out = {}
    for ref in refs:
        if ref.get('quote'):
            out.setdefault((ref.get('type'), ref.get('id'), ref['quote']), ref)
    return list(out.values())


def account_from_source(record):
    account = {k: deepcopy(record.get(k, '')) for k in ACCOUNT_FIELDS
               if k not in ('sources', '_user_fields', 'archived')}
    account['sources'] = references(record)
    account['_user_fields'] = [k for k in ACCOUNT_EDITABLE
                              if record.get('_provenance', {}).get(k, {}).get('mode') == 'user']
    account['archived'] = bool(record.get('archived'))
    return account


def contact_view(store, c, legacy):
    """Derived compatibility index for source lookups and old project links."""
    records = deepcopy(legacy)
    people = store._records(c, 'people')
    for record in records.values():
        if record.get('person_id') in people and 'accounts' in people[record['person_id']]:
            record['person_id'] = ''
    for pid, person in people.items():
        if person.get('archived'):
            continue
        for account in person.get('accounts') or []:
            rid = account['id']
            record = records.setdefault(rid, dict(id=rid, state='工作联系人', notes='', project_ids=[]))
            record.update(deepcopy(account), person_id=pid, candidate_person_id='',
                          revision=person['revision'], _person_revision=person['revision'],
                          _dossier_sources=deepcopy(account.get('sources') or []))
            record.setdefault('name', account.get('remark') or account.get('nickname') or person['name'])
    return records


def validate_accounts(store, c, value):
    if not isinstance(value, list) or len(value) > 50:
        raise StoreError('平台账号应为列表，每人最多50项。')
    existing = {a['id']: a for p in store._records(c, 'people').values()
                for a in p.get('accounts') or []}
    result, seen = [], set()
    for item in value:
        if not isinstance(item, dict) or set(item) - set(ACCOUNT_FIELDS):
            raise StoreError('账号资料格式不正确。')
        rid = item.get('id')
        if not isinstance(rid, str) or not rid or len(rid) > 160 or rid in seen:
            raise StoreError('账号标识无效或重复。')
        seen.add(rid)
        if item.get('platform') not in ('wechat', 'feishu', 'other'):
            raise StoreError('请选择微信、飞书或其他账号。')
        old = existing.get(rid)
        clean = deepcopy(old or dict(id=rid, platform=item['platform'], account_id='',
                                    source_scope='', sources=[], _user_fields=[], archived=False))
        for key in ('platform', 'account_id', 'source_scope'):
            if old and key in item and item[key] != old.get(key, ''):
                raise StoreError('来源账号身份由系统保留，不能在资料编辑中改写。')
            if not old and key in ('account_id', 'source_scope') and item.get(key):
                raise StoreError('新增联系方式请填写微信号或显示名；来源身份由采集匹配。')
        for key in ACCOUNT_EDITABLE:
            text = item.get(key, clean.get(key, ''))
            if not isinstance(text, str) or len(text) > (12000 if key == 'notes' else 500):
                raise StoreError('账号名称或备注格式不正确。')
            if text.strip() != clean.get(key, ''):
                clean['_user_fields'] = list(dict.fromkeys([*clean.get('_user_fields', []), key]))
            clean[key] = text.strip()
        if not any(clean.get(k) for k in ACCOUNT_EDITABLE):
            raise StoreError('请填写一个可识别的账号名称、备注或微信号。')
        if type(item.get('archived', clean.get('archived', False))) is not bool:
            raise StoreError('账号停用状态格式不正确。')
        clean['archived'] = item.get('archived', clean.get('archived', False))
        result.append(clean)
    return result


def check_account_ownership(store, c, pid, accounts):
    ids = {a['id'] for a in accounts}
    for other_id, person in store._records(c, 'people').items():
        if other_id == pid or person.get('archived'):
            continue
        if ids & {a['id'] for a in person.get('accounts') or []}:
            raise StoreError('此账号已属于另一份人员档案，请明确调整对应关系，不能重复归属。')


def validate_log(value):
    if not isinstance(value, list) or len(value) > 10000:
        raise StoreError('工作过程应为记录列表。')
    result, ids = [], set()
    allowed = {'id', 'text', 'date', 'date_note', 'recorded_at', 'mode', 'sources', 'origin', 'actor'}
    for entry in value:
        if not isinstance(entry, dict) or set(entry) - allowed:
            raise StoreError('工作过程格式不正确。')
        for key in ('id', 'text', 'date', 'date_note', 'recorded_at', 'mode', 'origin', 'actor'):
            if not isinstance(entry.get(key, ''), str):
                raise StoreError('工作过程内容应为文本。')
        if not entry.get('id') or entry['id'] in ids or not entry.get('text', '').strip():
            raise StoreError('工作过程标识重复，或内容为空。')
        if len(entry['text']) > 20000 or len(entry.get('date_note', '')) > 2000:
            raise StoreError('单次工作记录过长，请分段记录。')
        if entry.get('date'):
            try:
                dt.date.fromisoformat(entry['date'])
            except ValueError:
                raise StoreError('发生日期格式应为年-月-日；未知时可留空。') from None
        if entry.get('mode') not in ('fact', 'inference', 'user', 'migration'):
            raise StoreError('工作过程须区分事实、建议与用户记录。')
        if not isinstance(entry.get('sources', []), list):
            raise StoreError('工作过程依据格式不正确。')
        ids.add(entry['id'])
        result.append(deepcopy(entry))
    return result


def entry(text, *, date='', date_note='', sources=(), mode='fact', origin='', actor='Codex依据整理', key=None):
    identity = key or 'log-' + uuid.uuid4().hex[:20]
    return dict(id=identity, text=text, date=date, date_note=date_note, sources=list(sources),
                mode=mode, origin=origin, actor=actor, recorded_at=now())


class Work:
    def __init__(self, store):
        self.store = store

    def create_from_source(self, document):
        if not isinstance(document, dict) or set(document) != {'key', 'values', 'sources'}:
            raise StoreError('依据新建工作需提供稳定事项标识、工作内容和原文来源。')
        key = document['key']
        if not isinstance(key, str) or not key.strip() or len(key) > 200:
            raise StoreError('请给出能用于避免重复新建的具体事项标识。')
        values = document['values']
        if not isinstance(values, dict) or set(values) & {'work_log', 'archived'}:
            raise StoreError('新工作只填写已有依据的当前资料，不覆盖过程或归档状态。')
        rid = 'work-' + hashlib.sha256(key.encode()).hexdigest()[:24]
        digest = hashlib.sha256(json_text(document).encode()).hexdigest()
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            from task_work_links import TaskWorkLinks
            links_service = TaskWorkLinks(self.store)
            from people import People
            sources = People._refs(c, document['sources'])
            task_sources = {}
            for ref in sources:
                if ref.get('type') == 'raw':
                    raw = c.execute('SELECT source_id,object_kind FROM raw_versions WHERE id=?',(ref.get('id'),)).fetchone()
                    if raw and raw['object_kind']=='task':
                        task_sources[raw['source_id']] = ref['id']
            if len(task_sources)>1:
                raise StoreError('请先分别核对任务身份，已有工作应通过关联入口合并，不能一次新建多个任务来源。')
            if task_sources:
                sid, vid = next(iter(task_sources.items()))
                rid = 'work-' + hashlib.sha256(sid.encode()).hexdigest()[:24]
                linked = links_service.get(c,sid)
                if linked and not (linked['status']=='linked' and len(linked['links'])==1 and linked['links'][0]['work_id']==rid):
                    raise StoreError('此飞书任务已经关联工作或有待处理决定，不能重复新建；请读取原关联。',409)
            previous = self.store._records(c, 'tasks', True).get(rid)
            if previous:
                if previous.get('_creation_input_hash') != digest:
                    raise StoreError('这个事项已经存在，请读取现有工作并追加过程，不能换内容重新新建。', 409)
                return dict(id=rid, revision=previous['revision'], unchanged=True, protected=bool(previous.get('archived') or previous.get('_removed')))
            from work_proposals import exact_matches
            matches = exact_matches(self.store, c, values)
            if matches:
                raise StoreError('同项目已有同名工作，请读取并追加到现有事项：' + '、'.join(r['id'] for r in matches), 409)
            from people import People
            sources = People._refs(c, document['sources'])
            clean = self.store._validate(c, 'tasks', values, True)
            clean.setdefault('state', '待办')
            clean['_provenance'] = {k: dict(mode='fact', sources=sources, actor='Codex依据整理', at=now()) for k in clean}
            clean['_creation_input_hash'] = digest
            clean['_dossier_sources'] = sources
            self.store._check_hierarchy(c, 'tasks', rid, clean)
            saved = self.store._write(c, 'tasks', rid, clean, {'_removed': True}, 0,
                                     '依据新建工作', 'Codex依据整理', '明确的新事项；相同标识重复导入不会覆盖维护')
            if task_sources:
                links_service.bind(dict(version_id=vid,revision=0,links=[dict(work_id=rid,relation='same',
                    reason='核对现有工作后确认为明确独立事项，按飞书任务稳定身份建档。')],
                    reason='核对现有工作后确认为明确独立事项，首次建档并保存长期关联。'),connection=c)
            return saved

    def append(self, rid, revision, values, actor='用户', *, connection=None):
        """Append an event and its explicit current-state change in one transaction."""
        if not isinstance(values, dict) or set(values) - {'text', 'date', 'date_note', 'sources', 'mode', 'key', 'changes'}:
            raise StoreError('请提供本次工作记录及需要同步的当前情况。')
        if not isinstance(values.get('text'), str) or not values['text'].strip():
            raise StoreError('请填写本次发生的事情或处理结果。')
        if type(revision) is not int or revision < 0:
            raise StoreError('请先读取当前工作版本。')
        with (nullcontext(connection) if connection is not None else self.store.connect()) as c:
            if connection is None:
                c.execute('BEGIN IMMEDIATE')
            current = self.store._record(c, 'tasks', rid)
            if current['revision'] != revision:
                raise StoreError('工作已更新，本次输入仍保留，请重新读取后核对。', 409)
            if current.get('archived'):
                raise StoreError('工作已归档，请先恢复再继续记录。', 409)
            mode = values.get('mode', 'user')
            sources = []
            if mode in ('fact', 'inference'):
                from people import People
                sources = People._refs(c, values.get('sources'))
            elif mode != 'user':
                raise StoreError('请选择事实、建议或用户记录。')
            log = entry(values.get('text', '').strip(), date=values.get('date', ''),
                        date_note=values.get('date_note', ''), sources=sources, mode=mode,
                        actor=actor, key=values.get('key'))
            logs = list(current.get('work_log') or [])
            previous = next((r for r in logs if r['id'] == log['id']), None)
            if previous:
                if any(previous.get(k) != log.get(k) for k in ('text', 'date', 'date_note', 'mode', 'sources')):
                    raise StoreError('同一工作过程标识已有不同内容，不能覆盖原记录。', 409)
                return dict(id=rid, revision=revision, unchanged=True)
            changes = values.get('changes') or {}
            if not isinstance(changes, dict) or set(changes) - {'progress', 'result', 'next_action', 'state', 'priority', 'due_date', 'schedule_note'}:
                raise StoreError('本次记录只能同步当前情况、结果、下一步、状态、优先级和时间。')
            if mode == 'inference' and any(k in changes for k in ('state', 'priority', 'due_date')):
                raise StoreError('建议不能直接设定工作状态、优先级或期限；请保留建议，确需用户决定时提出具体问题。')
            if mode == 'user':
                return self.store.update_in_transaction(c, 'tasks', rid,
                    dict(changes, work_log=[*logs, log]), revision, actor, '追加工作处理记录')
            # Evidence never overrides a human-edited current value; its event is
            # still appended so new facts remain visible for a later decision.
            prior = c.execute("SELECT payload FROM wk_records WHERE kind='tasks' AND id=?", (rid,)).fetchone()
            before = json.loads(prior[0]) if prior else {}
            after = deepcopy(before)
            provenance = dict(after.get('_provenance') or {})
            protected, changed = [], []
            clean = self.store._validate(c, 'tasks', changes) if changes else {}
            for key, value in clean.items():
                human = provenance.get(key, {}).get('mode') == 'user' or (key in before and not provenance.get(key) and before[key] not in ('', None, []))
                if human and current.get(key) != value:
                    protected.append(key)
                    continue
                if current.get(key) != value:
                    after[key] = value
                    provenance[key] = dict(mode=mode, sources=sources, at=now(), actor=actor)
                    changed.append(key)
            after['work_log'] = validate_log([*logs, log])
            after['_provenance'] = provenance
            result = self.store._write(c, 'tasks', rid, after, before, revision, '依据追加工作记录', actor, '来源核对；人工当前情况受保护')
            return dict(result, changed=changed, protected=protected)


class Accounts:
    def __init__(self, store):
        self.store = store

    def seed(self, c, source):
        """Attach an evidence-verified source identity without replacing manual names."""
        pid = source.get('person_id')
        if not pid:
            return None  # Unmapped source metadata never creates a fictional person.
        person = self.store._record(c, 'people', pid)
        if person.get('archived'):
            raise StoreError('来源对应的人员已归档，不能自动重开。', 409)
        incoming = account_from_source(source)
        accounts = deepcopy(person.get('accounts') or [])
        if any(a['id'] == incoming['id'] for a in accounts):
            return None
        check_account_ownership(self.store, c, pid, [incoming])
        for other in self.store._records(c, 'people').values():
            if any((a.get('platform'), a.get('account_id'), a.get('source_scope', '')) ==
                   (incoming['platform'], incoming['account_id'], incoming.get('source_scope', ''))
                   for a in other.get('accounts') or []):
                raise StoreError('相同来源身份已对应人员，请复用现有账号。', 409)
        prior = c.execute("SELECT payload FROM wk_records WHERE kind='people' AND id=?", (pid,)).fetchone()
        before = json.loads(prior[0]) if prior else {}
        after = dict(before, accounts=[*accounts, incoming])
        return self.store._write(c, 'people', pid, after, before, person['revision'],
                                 '依据新增人员账号', 'Codex依据整理', '来源身份已核对；保留既有账号和人工名称')

    def save(self, pid, revision, account_id, values):
        if not isinstance(values, dict) or set(values) - {*ACCOUNT_EDITABLE, 'platform', 'archived'}:
            raise StoreError('只能修改账号名称、昵称、备注、微信号及停用状态。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            person = self.store._record(c, 'people', pid)
            accounts = deepcopy(person.get('accounts') or [])
            if account_id:
                account = next((a for a in accounts if a['id'] == account_id), None)
                if not account:
                    raise StoreError('该账号已不属于此人员，请重新读取。', 409)
                account.update(values)
            else:
                accounts.append(dict(id='account-' + uuid.uuid4().hex[:20], **values))
            return self.store.update_in_transaction(c, 'people', pid, {'accounts': accounts}, revision,
                                                    '用户', '在人员档案维护平台账号')

    def enrich(self, pid, revision, account_id, fields):
        if not isinstance(fields, dict) or not fields or set(fields) - set(ACCOUNT_EDITABLE):
            raise StoreError('来源可补齐账号显示资料，不能改写身份和对应关系。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            person = self.store._record(c, 'people', pid)
            if person['revision'] != revision:
                raise StoreError('人员资料已更新，请重新读取。', 409)
            if person.get('archived'):
                raise StoreError('人员已归档，来源更新不会重开。', 409)
            accounts = deepcopy(person.get('accounts') or [])
            account = next((a for a in accounts if a['id'] == account_id), None)
            if not account or account.get('archived'):
                raise StoreError('账号不存在或已停用，请先核对。', 409)
            protected, changed, sources = [], [], list(account.get('sources') or [])
            from people import People
            for key, field in fields.items():
                if not isinstance(field, dict) or set(field) != {'value','sources'}:
                    raise StoreError('每项账号资料须附原文依据。')
                value = field['value']
                if not isinstance(value, str) or len(value) > (12000 if key == 'notes' else 500):
                    raise StoreError('账号资料格式不正确。')
                refs = People._refs(c, field['sources'])
                if key in account.get('_user_fields', []):
                    protected.append(key)
                    continue
                if account.get(key, '') != value.strip():
                    account[key] = value.strip()
                    changed.append(key)
                    sources.extend(refs)
            if not changed:
                return dict(id=pid, revision=revision, unchanged=True, protected=protected)
            account['sources'] = list({json_text([s['type'],s['id'],s['quote']]): s for s in sources}.values())
            prior = c.execute("SELECT payload FROM wk_records WHERE kind='people' AND id=?", (pid,)).fetchone()
            before = json.loads(prior[0]) if prior else {}
            result = self.store._write(c, 'people', pid, dict(before, accounts=accounts), before, revision,
                                      '依据补齐人员账号', 'Codex依据整理', '逐账号保护人工名称、备注和停用状态')
            return dict(result, changed=changed, protected=protected)
