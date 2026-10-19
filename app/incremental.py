"""V44: evidence deltas drive review; saved attributes have no calendar expiry.

The model still interprets evidence. This module only scopes input, detects
concurrent edits, and checkpoints completed work. It never infers facts.
"""
import argparse
import json
import uuid
from pathlib import Path

from store import Store, StoreError, json_text, now
from work_analysis import digest

KEY = 'incremental_version'
KINDS = ('tasks', 'projects', 'people', 'organizations', 'departments')
ANALYSIS_CONTENT = ('current', 'next_step', 'next_key', 'timing_kind', 'timing_basis',
                    'rationale', 'sources', 'decision', 'completion_check')
PRIORITY_INSTRUCTIONS = ('已确认优先级持续有效，不因日期或一般进展重复判断。这里只返回首次未判断项、'
    '具体新事实已登记影响的对象、关联决定或引用依据确实变化的对象。人工设定优先。'
    '按当前责任、阻塞、真实影响和投入边界判断；不把缺资料视为低，也不把单项工作紧急自动上推整个项目。'
    'review_on可留空，旧复核日期不再触发重评。优先级仅影响排序，不更改状态、责任、目标、期限或提醒。')


def enabled(c):
    row = c.execute('SELECT value FROM wk_meta WHERE key=?', (KEY,)).fetchone()
    return bool(row and json.loads(row[0]) >= 44)


def source_delta(c):
    return bool(c.execute("SELECT 1 FROM wk_meta WHERE key='incremental_source_delta' AND value='1'").fetchone())


def pending_source_edit(c, refs):
    for ref in refs:
        if ref.get('type') != 'raw':
            continue
        row = c.execute('''SELECT h.version_id FROM raw_versions v
            JOIN raw_heads h ON h.source_id=v.source_id AND h.object_id=v.object_id
            LEFT JOIN wk_review_receipts r ON r.version_id=h.version_id
            WHERE v.id=? AND h.version_id<>v.id AND r.version_id IS NULL''', (ref['id'],)).fetchone()
        if row:
            return True
    return False


def analysis_content(value):
    return {k: value.get(k) for k in ANALYSIS_CONTENT}


def provenance(value):
    # Keep authority and evidence, omit timestamps/actors that do not alter facts.
    return {k: {f: v for f, v in p.items() if f not in ('at', 'actor', 'updated_at')}
            for k, p in value.items()}


def priority_token(env, kind, record, assessment):
    refs = list(assessment.get('sources', [])) + list(assessment.get('timing', {}).get('sources', []))
    return digest([kind, record['id'], record.get('priority'),
                   provenance({'priority': record.get('_provenance', {}).get('priority', {})}),
                   env.get('priority_impacts', {}).get((kind, record['id']), 0),
                   [(qid, env['cards'].get(qid)) for qid in assessment.get('judgment_ids', [])],
                   sorted((s['id'], env['raw'].get(s['id'])) for s in refs if s['type'] == 'raw')])


class Incremental:
    def __init__(self, store):
        self.store = store

    def initialize(self):
        with self.store.connect() as c:
            c.executescript('''
              CREATE TABLE IF NOT EXISTS wk_incremental_runs(
                id TEXT PRIMARY KEY, state TEXT NOT NULL, snapshot TEXT NOT NULL,
                created_at TEXT NOT NULL, finished_at TEXT);
              CREATE TABLE IF NOT EXISTS wk_incremental_priority_impacts(
                kind TEXT NOT NULL,record_id TEXT NOT NULL,generation INTEGER NOT NULL,
                reason TEXT NOT NULL,sources TEXT NOT NULL,PRIMARY KEY(kind,record_id));
              CREATE TABLE IF NOT EXISTS wk_incremental_priority_impact_events(
                id INTEGER PRIMARY KEY,kind TEXT NOT NULL,record_id TEXT NOT NULL,
                generation INTEGER NOT NULL,reason TEXT NOT NULL,sources TEXT NOT NULL,happened_at TEXT NOT NULL);
            ''')
            if 'receipt' not in {r['name'] for r in c.execute('PRAGMA table_info(wk_incremental_runs)')}:
                c.execute("ALTER TABLE wk_incremental_runs ADD COLUMN receipt TEXT NOT NULL DEFAULT ''")

    def snapshot(self, c):
        from model import FIELDS
        records = {}
        from work_completion import WorkCompletion
        relations = {}
        if c.execute("SELECT 1 FROM sqlite_master WHERE name='wk_person_relations'").fetchone():
            for row in c.execute('SELECT * FROM wk_person_relations ORDER BY id'):
                relations.setdefault(row['person_id'], []).append({k: row[k] for k in row.keys() if k not in ('updated_at','revision')})
        for kind in KINDS:
            records[kind] = {rid: digest({**{k: r.get(k) for k in FIELDS[kind]},
                            '_provenance': provenance(r.get('_provenance', {})),
                            'goal': WorkCompletion.goal(c, rid) if kind == 'tasks' else None,
                            'relations': relations.get(rid, []) if kind == 'people' else None,
                            'analysis': analysis_content(r.get('_work_analysis', {}))})
                            for rid, r in self.store._records(c, kind).items()}
        cards = {r['id']: digest(dict(r)) for r in c.execute('SELECT * FROM wk_judgment_cards')}
        authority = digest([tuple(r) for r in c.execute('SELECT id,state,revision FROM raw_sources ORDER BY id')])
        heads = digest([tuple(r) for r in c.execute('SELECT * FROM raw_heads ORDER BY source_id,object_id')])
        impacts = digest([tuple(r) for r in c.execute('SELECT * FROM wk_incremental_priority_impacts ORDER BY kind,record_id')])
        controls = digest([tuple(r) for r in c.execute('SELECT * FROM wk_reminder_controls ORDER BY work_id')])
        return dict(records=records, cards=cards, source_authority=authority,
                    evidence_heads=heads, priority_impacts=impacts, reminder_controls=controls)

    @staticmethod
    def delta(previous, current):
        return ({kind: [rid for rid in sorted(set(current['records'][kind]) | set(previous['records'].get(kind, {})))
                        if previous['records'].get(kind, {}).get(rid) != current['records'][kind].get(rid)] for kind in KINDS},
                [qid for qid in sorted(set(current['cards']) | set(previous['cards']))
                 if previous['cards'].get(qid) != current['cards'].get(qid)])

    def enable(self):
        """Rebase only already-current reviews, atomically, retaining business data.

        Carry real pending work forward. No stale finding is waived.
        The caller takes a SQLite backup before invoking this administrative step.
        """
        self.initialize()
        from work_analysis import WorkAnalysis
        from follow_through import FollowThrough
        from work_priority import WorkPriority
        from project_priority import ProjectPriority
        from work_reminders import WorkReminders
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            initial = not enabled(c)
            if enabled(c) and source_delta(c):
                return {'enabled': True, 'already_enabled': True}
            analysis, follow = WorkAnalysis(self.store), FollowThrough(self.store)
            tasks = self.store._records(c, 'tasks')
            projects = self.store._records(c, 'projects')
            valid_analyses = set()
            for r in tasks.values():
                view = analysis.view(c, r, projects.get(r.get('project_id'), {}), today='1900-01-01')
                if not view['needs_review']:
                    valid_analyses.add(r['id'])
            valid_follow = {(r['kind'], r['id']) for r in follow.rows(c) if not r['needs_review']}
            valid_priority = {}
            for kind, service, records in (('tasks', WorkPriority(self.store), tasks), ('projects', ProjectPriority(self.store), projects)):
                env = service.context(c)
                valid_priority[kind] = {rid for rid, r in records.items()
                    if not service.view(c, r, env, today='1900-01-01')['needs_review']}
            reminders = WorkReminders(self.store)
            valid_plans = []
            for row in c.execute('SELECT * FROM wk_reminder_plans'):
                r = tasks.get(row['work_id'])
                if r and row['context_token'] == reminders._context(c, r, projects.get(r.get('project_id'), {})):
                    valid_plans.append(row['work_id'])
            c.execute('INSERT OR REPLACE INTO wk_meta VALUES(?,?)', (KEY, '44'))
            c.execute("INSERT OR REPLACE INTO wk_meta VALUES('incremental_source_delta','1')")
            for rid, r in tasks.items():
                if r.get('_work_analysis') and rid in valid_analyses:
                    # Stored payload only: preserve revision, audit history and user fields.
                    row = c.execute("SELECT payload FROM wk_records WHERE kind='tasks' AND id=?", (rid,)).fetchone()
                    payload = json.loads(row[0])
                    payload['_work_analysis']['context_token'] = analysis.context_token(c, r, projects.get(r.get('project_id'), {}))
                    c.execute("UPDATE wk_records SET payload=? WHERE kind='tasks' AND id=?", (json_text(payload), rid))
            env = follow.context(c)
            for (kind, rid) in env['reviews']:
                if (kind, rid) in valid_follow:
                    c.execute('UPDATE wk_followthrough_reviews SET input_token=? WHERE kind=? AND record_id=?',
                              (follow.token(env, kind, rid), kind, rid))
            for service, table, column, kind in ((WorkPriority(self.store), 'wk_work_priority_reviews', 'work_id', 'tasks'),
                    (ProjectPriority(self.store), 'wk_project_priority_reviews', 'project_id', 'projects')):
                env = service.context(c)
                for row in c.execute('SELECT * FROM ' + table).fetchall():
                    r = env['records'][kind].get(row[column])
                    if r and row[column] in valid_priority[kind]:
                        c.execute(f'UPDATE {table} SET context_token=? WHERE {column}=?',
                                  (service.token(env, r, json.loads(row['payload'])), row[column]))
            tasks = self.store._records(c, 'tasks')
            for rid in valid_plans:
                r = tasks[rid]
                c.execute('UPDATE wk_reminder_plans SET context_token=? WHERE work_id=?',
                          (reminders._context(c, r, projects.get(r.get('project_id'), {})), rid))
            snapshot = self.snapshot(c)
            if initial:
                c.execute('INSERT OR REPLACE INTO wk_meta VALUES(?,?)', ('incremental_checkpoint', json_text(snapshot)))
            c.execute('INSERT OR REPLACE INTO wk_meta VALUES(?,?)', ('incremental_migration', json_text(dict(at=now(), plans=len(valid_plans), version=44))))
        return {'enabled': True, 'version': 44, 'preserved_reminder_plans': len(valid_plans)}

    def priority_impact(self, document):
        self.initialize()
        kind, rid = document['kind'], document['id']
        reason, sources = document['reason'], document['sources']
        if kind not in ('tasks', 'projects') or not isinstance(reason, str) or len(reason.strip()) < 8 or not sources:
            raise StoreError('请说明改变原优先级依据的具体事实和来源。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if not enabled(c):
                raise StoreError('尚未启用增量整理。')
            self.store._record(c, kind, rid)
            from raw_store import RawStore
            for vid in sources:
                row = c.execute('SELECT source_id FROM raw_versions WHERE id=?', (vid,)).fetchone()
                if not row:
                    raise StoreError('优先级变化依据不存在。')
                RawStore(self.store).source(row[0], True, c)
            old = c.execute('SELECT * FROM wk_incremental_priority_impacts WHERE kind=? AND record_id=?', (kind, rid)).fetchone()
            if old and old['reason'] == reason and old['sources'] == json_text(sources):
                return {'registered': False, 'generation': old['generation']}
            generation = old['generation'] + 1 if old else 1
            c.execute('INSERT OR REPLACE INTO wk_incremental_priority_impacts VALUES(?,?,?,?,?)',
                      (kind, rid, generation, reason, json_text(sources)))
            c.execute('INSERT INTO wk_incremental_priority_impact_events(kind,record_id,generation,reason,sources,happened_at) VALUES(?,?,?,?,?,?)',
                      (kind, rid, generation, reason, json_text(sources), now()))
        return {'registered': True, 'generation': generation}

    def prepare(self, limit=200):
        from periodic_update import Periodic
        self.initialize()
        with self.store.connect() as c:
            if not enabled(c):
                raise StoreError('请先启用增量整理。')
        packet = Periodic(self.store).packet(limit, compact=True)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            current = self.snapshot(c)
            old = c.execute("SELECT value FROM wk_meta WHERE key='incremental_checkpoint'").fetchone()
            previous = json.loads(old[0]) if old else {'records': {}, 'cards': {}}
            changed, card_ids = self.delta(previous, current)
            cards = [dict(r) for r in c.execute('SELECT * FROM wk_judgment_cards') if r['id'] in card_ids]
            for r in cards:
                r['payload'] = json.loads(r['payload'])
            packet['changed_entities'] = changed
            packet['changed_decisions'] = cards
            packet['entity_index'] = {kind: [{k: r.get(k) for k in ('id', 'name', 'organization_id', 'role')}
                                      for r in self.store._records(c, kind).values() if not r.get('archived')]
                                      for kind in ('people', 'organizations')}
            packet['run_id'] = 'incremental-' + uuid.uuid4().hex
            c.execute('INSERT INTO wk_incremental_runs(id,state,snapshot,created_at,finished_at) VALUES(?,?,?,?,NULL)',
                      (packet['run_id'], 'prepared', json_text(current), now()))
        if not packet['items'] and not any(changed.values()) and not cards:
            packet['work_index'] = []
            packet['project_index'] = []
            packet['entity_index'] = {}
        packet['instructions'] = ('只判断未整理的新版本、修订版本及本轮变化的决定和主体。先关联原工作，'
            '只读相关工作的目标、事实及必要上下文；明确变化写回工作，再汇总受影响项目。'
            '人员、组织、角色、优先级等已确认属性无新依据不复核。新事实确实推翻优先级依据时，'
            '先priority-impact登记具体原因与原文，再只评估该项。保留真实到期提醒、用户改时、暂停、'
            '完成取消及送达回执检查；内部复核日不触发全库分析。无新事实的关联工作留无变化理由，'
            '不重复保存分析。原文版本和人工答复均须实际落实，最后登记原回执并checkpoint。')
        return packet

    def pending(self):
        """Return only IDs that actually require work; no repeated nested records."""
        from work_analysis import WorkAnalysis
        from follow_through import FollowThrough
        from work_priority import WorkPriority
        from project_priority import ProjectPriority
        from work_reminders import WorkReminders
        with self.store.connect() as c:
            c.execute('BEGIN')
            env = FollowThrough(self.store).context(c)
            a = WorkAnalysis(self.store)
            result = {'analysis': [rid for rid, r in env['records']['tasks'].items()
                if a.view(c, r, env['records']['projects'].get(r.get('project_id'), {}))['needs_review']],
                'follow_through': [{k:r[k] for k in ('kind','id','reason')}
                    for r in FollowThrough(self.store).rows(c, env) if r['needs_review']]}
            for kind, service, key in (('tasks', WorkPriority(self.store), 'work_priority'),
                                       ('projects', ProjectPriority(self.store), 'project_priority')):
                context = service.context(c, env)
                result[key] = [rid for rid, r in env['records'][kind].items() if service.view(c,r,context)['needs_review']]
        result['reminder_plans'] = [r['id'] for r in WorkReminders(self.store).packet()['items']]
        return result

    def settle(self, run_id):
        """Read only: give the exact current delta for final semantic acknowledgment."""
        with self.store.connect() as c:
            c.execute('BEGIN')
            if not c.execute('SELECT 1 FROM wk_incremental_runs WHERE id=?', (run_id,)).fetchone():
                raise StoreError('增量批次不存在。')
            current = self.snapshot(c)
            prior = json.loads(c.execute("SELECT value FROM wk_meta WHERE key='incremental_checkpoint'").fetchone()[0])
            entities, cards = self.delta(prior, current)
            return dict(run_id=run_id, snapshot_token=digest(current), changed_entities=entities,
                        changed_decisions=cards)

    def checkpoint(self, document):
        run_id = document['run_id']
        with self.store.connect() as c:
            row = c.execute('SELECT state,receipt FROM wk_incremental_runs WHERE id=?', (run_id,)).fetchone()
            if row and row['state'] == 'finished':
                if row['receipt'] != json_text(document):
                    raise StoreError('该批次已有不同完成回执，不能覆盖。', 409)
                return dict(run_id=run_id, finished=True, unchanged=True)
        from periodic_update import Periodic
        check = Periodic(self.store).review_check()
        if not check['ready']:
            raise StoreError('本轮仍有未落实内容：' + json_text(check['checks']), 409)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM wk_incremental_runs WHERE id=?', (run_id,)).fetchone()
            if not row:
                raise StoreError('增量批次不存在。')
            current = self.snapshot(c)
            if digest(current) != document.get('snapshot_token'):
                raise StoreError('整理期间资料或人工决定又有变化，请只核对新增差异。', 409)
            prior = json.loads(c.execute("SELECT value FROM wk_meta WHERE key='incremental_checkpoint'").fetchone()[0])
            entities, cards = self.delta(prior, current)
            required = {(kind, rid) for kind, ids in entities.items() for rid in ids} | {('decisions', qid) for qid in cards}
            acknowledgments = document.get('acknowledgments', [])
            keys = [(r.get('kind'), r.get('id')) for r in acknowledgments]
            if set(keys) != required or len(keys) != len(set(keys)) or any(len(r.get('reason', '').strip()) < 8 for r in acknowledgments):
                raise StoreError('须逐项说明本轮变化的主体和用户答复已如何落实；未变化主体不复核。')
            if c.execute('''SELECT 1 FROM raw_versions v JOIN raw_sources s ON s.id=v.source_id
                LEFT JOIN wk_review_receipts r ON r.version_id=v.id
                WHERE s.state='approved' AND r.version_id IS NULL LIMIT 1''').fetchone():
                raise StoreError('有新的未整理版本，检查点未前移。', 409)
            if row['state'] != 'finished':
                newer = c.execute("SELECT 1 FROM wk_incremental_runs WHERE state='finished' AND rowid>(SELECT rowid FROM wk_incremental_runs WHERE id=?)", (run_id,)).fetchone()
                if newer:
                    raise StoreError('已有较新批次完成，不能回退检查点。', 409)
                c.execute('INSERT OR REPLACE INTO wk_meta VALUES(?,?)', ('incremental_checkpoint', json_text(current)))
                c.execute("UPDATE wk_incremental_runs SET state='finished',finished_at=? WHERE id=?", (now(), run_id))
                c.execute('UPDATE wk_incremental_runs SET receipt=? WHERE id=?', (json_text(document), run_id))
                c.execute('INSERT OR REPLACE INTO wk_meta VALUES(?,?)', ('incremental_last_receipt', json_text(document)))
        return dict(run_id=run_id, finished=True, checks=check['checks'])


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--db')
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('enable')
    sub.add_parser('pending')
    q = sub.add_parser('prepare'); q.add_argument('--limit', type=int, default=200); q.add_argument('--output', required=True)
    q = sub.add_parser('priority-impact'); q.add_argument('file')
    q = sub.add_parser('settle'); q.add_argument('run_id')
    q = sub.add_parser('checkpoint'); q.add_argument('file')
    a = p.parse_args(); service = Incremental(Store(a.db))
    try:
        if a.command == 'enable': result = service.enable()
        elif a.command == 'pending': result = service.pending()
        elif a.command == 'prepare':
            result = service.prepare(a.limit)
            Path(a.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            result = dict(path=str(Path(a.output).resolve()), run_id=result['run_id'], packet_id=result['packet_id'],
                          versions=len(result['items']), changed_entities=result['changed_entities'], decisions=len(result['changed_decisions']))
        elif a.command == 'priority-impact': result = service.priority_impact(json.loads(Path(a.file).read_text('utf-8-sig')))
        elif a.command == 'settle': result = service.settle(a.run_id)
        else: result = service.checkpoint(json.loads(Path(a.file).read_text('utf-8-sig')))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except StoreError as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False)); raise SystemExit(1)


if __name__ == '__main__':
    main()
