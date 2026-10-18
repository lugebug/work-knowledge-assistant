"""Keep evidence-backed information questions in step with their real records."""
from copy import deepcopy
import json


PLACEHOLDERS = {'未指定', '未指定负责人', '未设置', '待定', '待补充', '待确认', '未知', '不详',
                '未明确', '暂不知道', '不知道', '尚未确定', '不适用', '-', '—', 'tbd'}


def meaningful(value):
    if isinstance(value, str):
        return bool(value.strip()) and value.strip().rstrip('。．. ').lower() not in PLACEHOLDERS
    return bool(value)


def known(record, field):
    return meaningful(record.get(field)) and record.get('_provenance', {}).get(field, {}).get('mode') != 'inference'


def requirements(card):
    """Legacy V30 questions have the same bounded meaning as explicit conditions."""
    if card.get('completion'):
        return card['completion']
    if card.get('category') != 'essential' or not card.get('id', '').startswith('project-responsibility-'):
        return []
    operations = card['options'][0]['operations']
    if len(operations) != 1 or operations[0]['kind'] != 'projects':
        return []
    operation = operations[0]
    fields = set(operation['values'])
    if fields - {'owner_id', 'owner', 'my_role'}:
        return []
    result = []
    if fields & {'owner_id', 'owner'}:
        result.append(dict(kind='projects', id=operation['id'], any_of=['owner_id', 'owner'], label='本阶段负责人'))
    if 'my_role' in fields:
        result.append(dict(kind='projects', id=operation['id'], any_of=['my_role'], label='我的参与方式'))
    return result


def validate_completion(card):
    from store import StoreError
    if 'completion' not in card:
        return
    conditions = card['completion']
    if card['category'] != 'essential' or not isinstance(conditions, list) or not 1 <= len(conditions) <= 8:
        raise StoreError('随记录补齐的条件只用于有明确依据的信息缺口。')
    allowed = {'projects': ({'owner_id', 'owner'}, {'my_role'}),
               'tasks': ({'owner_id', 'owner'}, {'owner_id', 'owner', 'participant_ids'})}
    covered = set()
    for item in conditions:
        if not isinstance(item, dict) or set(item) != {'kind', 'id', 'any_of', 'label'}:
            raise StoreError('完成条件需要关联档案、等价资料和可读名称。')
        fields = item['any_of']
        if (item['kind'] not in allowed or not isinstance(item['id'], str)
                or not isinstance(item['label'], str) or not item['label'].strip()
                or not isinstance(fields, list) or any(not isinstance(k, str) for k in fields)
                or set(fields) not in allowed[item['kind']] or len(fields) != len(set(fields))):
            raise StoreError('完成条件仅支持项目分工、本人角色和工作承接，不能自动作出业务取舍。')
        for field in fields:
            key = (item['kind'], item['id'], field)
            if key in covered:
                raise StoreError('完成条件不能重复覆盖同一资料。')
            covered.add(key)
    for option in card['options']:
        for operation in option['operations']:
            if any((operation['kind'], operation['id'], key) not in covered for key in operation['values']):
                raise StoreError('自动补齐问题不能夹带其他业务改动。')
        if any(not any(op['kind'] == item['kind'] and op['id'] == item['id'] and set(op['values']) & set(item['any_of'])
                       for op in option['operations']) for item in conditions):
            raise StoreError('每个完成条件须有对应的实际写回内容。')


def condition_status(store, conn, card):
    items = []
    cache = {}
    for condition in requirements(card):
        kind, rid = condition['kind'], condition['id']
        if kind not in cache:
            cache[kind] = store._records(conn, kind)
        record = cache[kind].get(rid)
        available = bool(record and not record.get('archived') and not record.get('_removed'))
        items.append({**condition, 'available': available,
                      'satisfied': bool(available and any(known(record, f) for f in condition['any_of'])),
                      'values': {f: (record or {}).get(f, '') for f in condition['any_of']}})
    return items


def project_view(store, conn, card):
    """Remove writes to filled fields while preserving stable local-editor indices."""
    if card.get('state') not in ('pending', 'deferred'):
        return card
    conditions = condition_status(store, conn, card)
    complete = [v for v in conditions if v['satisfied']]
    if not complete:
        return card
    result = deepcopy(card)
    locked = {(v['kind'], v['id'], field): v['values'][field] for v in complete for field in v['any_of']}
    for option in result['options']:
        for operation in option['operations']:
            operation['values'] = {field:value for field,value in operation['values'].items()
                                   if (operation['kind'], operation['id'], field) not in locked}
    for spec in result.get('editable') or []:
        operation = result['options'][0]['operations'][spec['operation']]
        key = operation['kind'], operation['id'], spec['field']
        if key in locked:
            spec.update(locked=True, current_value=locked[key])
    result['record_sync'] = {'completed': complete, 'remaining': [v for v in conditions if not v['satisfied']]}
    remaining = '、'.join(dict.fromkeys(v['label'] for v in result['record_sync']['remaining']))
    completed = '、'.join(dict.fromkeys(v['label'] for v in complete))
    if remaining:
        result['question'] = '、'.join(t['name'] for t in card['targets']) + '：请明确' + remaining
        result['recommendation'] = '已有记录已明确' + completed + '。本次只确认或修改仍待明确的' + remaining + '，具体建议见下方写回预览。'
        result['unknowns'] = '仍需明确：' + remaining + '。已补齐内容按当前档案保留，不再写入旧建议。'
    else:
        result['recommendation'] = '相关资料已补齐，等待完成记录同步。'
    return result


def reconcile_in_transaction(store, conn, changed=None):
    """Close only fulfilled information gaps; never reopen or decide business choices."""
    from judgment_cards import JudgmentCards
    from store import json_text, now
    if not JudgmentCards.exists(conn):
        return []
    resolved = []
    for row in conn.execute("SELECT * FROM wk_judgment_cards WHERE state IN ('pending','deferred')").fetchall():
        card = json.loads(row['payload'])
        conditions = requirements(card)
        if not conditions or changed and not any((v['kind'], v['id']) in changed for v in conditions):
            continue
        current = condition_status(store, conn, card)
        if not all(item['satisfied'] for item in current):
            continue
        # Completion records existing facts or explicit maintenance; it accepts no old proposal.
        answer = json_text({'option':'已随记录补齐', 'note':'关联档案已明确全部必要信息；旧建议未再次写入。',
                            'mode':'record_sync', 'changes':[], 'current':current})
        conn.execute("UPDATE wk_judgment_cards SET state='resolved',answer=?,deferred_until='',revision=revision+1,updated_at=? WHERE id=?",
                     (answer, now(), row['id']))
        after = dict(conn.execute('SELECT * FROM wk_judgment_cards WHERE id=?', (row['id'],)).fetchone())
        JudgmentCards._event(conn, row['id'], '随记录补齐', dict(row), after, '记录同步')
        # Receipts close only after all questions associated with that exact source version are fulfilled.
        for vid in card.get('receipt_ids') or []:
            siblings = [json.loads(r['payload']).get('receipt_ids', []) for r in conn.execute("SELECT payload FROM wk_judgment_cards WHERE state IN ('pending','deferred')")]
            if not any(vid in ids for ids in siblings) and conn.execute('SELECT 1 FROM wk_review_receipts WHERE version_id=?', (vid,)).fetchone():
                conn.execute('INSERT OR IGNORE INTO wk_review_resolutions VALUES(?,?,?)',
                             (vid, '关联档案补齐必要信息：' + card['question'], now()))
        resolved.append(row['id'])
    return resolved
