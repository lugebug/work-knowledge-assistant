"""Evidence-assisted writes; atomic, revision checked, human edits protected."""
import json
from store import StoreError, now
from work_sources import resolve_reference

def untouched_migration_field(conn, kind, rid, before, key):
    """Recognize only V4 generated defaults, never an intervening human edit."""
    if kind != 'projects' or key not in ('my_role', 'progress', 'resources') or before.get('_schema_migration') != 'unified-projects-v4':
        return False
    row = conn.execute("SELECT before_payload,after_payload FROM wk_changes WHERE kind=? AND record_id=? AND action='合并项目' ORDER BY id DESC LIMIT 1", (kind, rid)).fetchone()
    if not row or json.loads(row['after_payload']) != before:
        return False
    original = json.loads(row['before_payload'])
    return not original.get(key) and key not in before.get('_provenance', {})

def apply(store, kind, rid, revision, fields, reason):
    store._kind(kind)
    if type(revision) is not int or revision < 0 or not isinstance(fields, dict) or not fields:
        raise StoreError('需要当前版本和逐项依据。')
    with store.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        from work_model import enabled
        if enabled(conn) and kind in ('contacts', 'updates'):
            raise StoreError('账号随人员维护，过程随工作追加；旧独立写入口已停用。', 409)
        if any(key in fields for key in ('accounts', 'work_log', 'context_history')):
            raise StoreError('账号和过程记录请使用专门的追加维护入口，不能覆盖历史。')
        current = store._records(conn, kind, True).get(rid)
        if not current:
            raise StoreError('依据整理可更新现有档案；新建其他类别请使用create入口。')
        if current and current.get('_removed'):
            raise StoreError('已撤销的事项不能被自动重新创建。', 409)
        if (current or {}).get('revision', 0) != revision:
            raise StoreError('记录已更新，请重新读取后合并。', 409)
        prior = conn.execute('SELECT payload FROM wk_records WHERE kind=? AND id=?', (kind,rid)).fetchone()
        before = json.loads(prior[0]) if prior else ({} if current else {'_removed': True})
        after = dict(before) if current else {}
        provenance = dict(after.get('_provenance', {}))
        changed, protected = {}, []
        from completeness import protected_dimension_fields
        not_applicable=protected_dimension_fields(conn,kind,rid)
        for key, entry in fields.items():
            if not isinstance(entry, dict) or set(entry) != {'value','mode','sources'} or entry['mode'] not in ('fact','inference'):
                raise StoreError('逐项内容须注明事实或建议，以及原始依据。')
            if enabled(conn) and kind == 'tasks' and entry['mode'] == 'inference' and key in ('state', 'priority', 'due_date'):
                raise StoreError('建议不能直接设定工作状态、优先级或期限；确需取舍时提出具体问题。')
            if kind == 'projects' and key == 'priority':
                raise StoreError('项目优先级分析请走项目优先级核对入口；此处只保存用户明确设定。')
            if key in ('decision', 'decision_reason') or key == 'state' and entry['value'] in ('已完成','已发布','已采纳') and not (kind == 'tasks' and entry['mode'] == 'fact' and enabled(conn)):
                raise StoreError('自动整理不能代替用户作投入决定或确认完成。')
            clean = store._validate(conn,kind,{key:entry['value']})
            if key in not_applicable:
                protected.append(key)
                continue
            if not isinstance(entry['sources'], list) or not 1 <= len(entry['sources']) <= 8:
                raise StoreError('每项需1至8条可核对依据。')
            try:
                sources = [resolve_reference(conn, ref) for ref in entry['sources']]
            except ValueError as exc:
                raise StoreError(str(exc)) from None
            if current and key in before and provenance.get(key, {}).get('mode') not in ('fact','inference') and (provenance.get(key, {}).get('mode') == 'user' or before[key] not in ('', [], None)) and not untouched_migration_field(conn, kind, rid, before, key):
                protected.append(key)
                continue
            value = clean[key]
            decision_basis = {'goal','boundary','primary_type','work_types','resources','milestones','customer_ids','channel_ids','partner_ids','request_summary','expected_output','recommendation','cost_estimate','quote_reference','effort_estimate','calendar_estimate','dependencies','support_mode','project_id','organization_id','demand_ids','capability_ids','decision_needed'}
            if current and current.get('decision') and key in decision_basis and current.get(key, '') != value:
                protected.append(key)
                continue
            old = provenance.get(key, {})
            if current and current.get(key, '') == value and old.get('mode') == entry['mode'] and old.get('sources') == sources:
                continue
            changed[key] = value
            provenance[key] = dict(mode=entry['mode'], sources=sources, at=now(), actor='Codex依据整理')
        if not changed:
            return dict(id=rid, revision=revision, unchanged=True, protected=protected)
        after.update(changed)
        after['_provenance'] = provenance
        after.setdefault('state', (current or {}).get('state',''))
        store._validate(conn,kind,{k:v for k,v in after.items() if not k.startswith('_')}, not current)
        store._check_hierarchy(conn,kind,rid,changed)
        result = store._write(conn,kind,rid,after,before,revision,'依据整理','Codex',reason,created=not current)
        return dict(result, protected=protected, changed=list(changed))
