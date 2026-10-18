"""Shared local knowledge-store operations for the workbench and Codex CLI.

Collected evidence and curated baseline tables are read-only here. All writes go
to wk_records/wk_changes, so a source refresh cannot silently erase user edits.
"""
from __future__ import annotations

import datetime as dt
from contextlib import contextmanager, closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from runtime_config import database_path
DEFAULT_DB = database_path()
from model import KINDS, STATES, FIELDS, REQUIRED, RELATIONS, PROJECT_TYPES, ORG_ROLES, TASK_TYPES, PROJECT_MODES, DEPARTMENT_TYPES



class StoreError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def now() -> str:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class Store:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.environ.get("WORK_KNOWLEDGE_DB", DEFAULT_DB)).resolve()
        if not self.path.is_file():
            raise StoreError("找不到现有知识库；不会自动创建一个空库。", 503)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def initialize(self):
        with self.connect() as conn:
            needed = {"projects", "messages", "evidence", "product_capabilities", "product_roadmap", "product_demands"}
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not needed <= tables:
                raise StoreError("知识库结构不完整，请运行安装检查并从备份恢复。", 503)
            if "wk_records" not in tables:
                backup_dir = self.path.parent / "backups"
                backup_dir.mkdir(exist_ok=True)
                backup = backup_dir / ("pilot_before_workbench_" + dt.datetime.now().strftime("%Y%m%d_%H%M%S") + ".db")
                with closing(sqlite3.connect(backup)) as dest:
                    conn.backup(dest)
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS wk_records(
                    kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                    revision INTEGER NOT NULL, updated_at TEXT NOT NULL,
                    created INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS wk_changes(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL,
                    record_id TEXT NOT NULL, action TEXT NOT NULL,
                    before_payload TEXT NOT NULL, after_payload TEXT NOT NULL,
                    before_revision INTEGER NOT NULL, after_revision INTEGER NOT NULL,
                    happened_at TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
                    undo_of INTEGER REFERENCES wk_changes(id),
                    before_view TEXT NOT NULL DEFAULT '{}', after_view TEXT NOT NULL DEFAULT '{}');
                CREATE INDEX IF NOT EXISTS wk_changes_record ON wk_changes(kind,record_id,id);
                CREATE TABLE IF NOT EXISTS wk_catalog(kind TEXT NOT NULL,id TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(kind,id));
                CREATE TABLE IF NOT EXISTS wk_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(wk_changes)")}
            for column in ["before_view", "after_view"]:
                if column not in columns:
                    conn.execute("ALTER TABLE wk_changes ADD COLUMN " + column + " TEXT NOT NULL DEFAULT '{}'")
            from people import People
            has_people = People.exists(conn)
        if has_people: People(self).initialize()
        from judgment_cards import JudgmentCards
        JudgmentCards(self).initialize()
        from work_proposals import WorkProposals
        WorkProposals(self).initialize()
        from work_lifecycle import WorkLifecycle
        WorkLifecycle(self).initialize()
        from work_completion import WorkCompletion
        WorkCompletion(self).initialize()
        from work_priority import WorkPriority
        WorkPriority(self).initialize()
        from project_priority import ProjectPriority
        ProjectPriority(self).initialize()
        from task_work_links import TaskWorkLinks
        TaskWorkLinks(self).initialize()
        from follow_through import FollowThrough
        FollowThrough(self).initialize()

    @staticmethod
    def _kind(kind):
        if kind not in KINDS:
            raise StoreError("不支持的数据类别。", 404)

    def _base(self, conn, kind):
        self._kind(kind)
        out = {}
        table = {"contacts": "contact_candidates", "capabilities": "product_capabilities",
                 "roadmap": "product_roadmap", "demands": "product_demands"}.get(kind, kind)
        rows = [] if kind in ("tasks", "updates", "people", "organizations", "departments") else conn.execute("SELECT * FROM " + table).fetchall()
        for row in rows:
            item = dict(row)
            if kind == "contacts":
                item["project_ids"] = json.loads(item["project_ids"])
                item["evidence_ids"] = json.loads(item["evidence_ids"])
            item.update(revision=0, updated_at="", manual=False, archived=False,
                        state=STATES[kind][0], notes="")
            out[item["id"]] = item
        if conn.execute("SELECT 1 FROM sqlite_master WHERE name='wk_catalog'").fetchone():
            for row in conn.execute("SELECT id,payload FROM wk_catalog WHERE kind=?", (kind,)):
                item = out.setdefault(row['id'], dict(id=row['id'], revision=0, updated_at='', manual=False, archived=False, state=STATES[kind][0], notes=''))
                item.update(json.loads(row['payload']))
        return out

    def _records(self, conn, kind, include_removed=False):
        records = self._base(conn, kind)
        for row in conn.execute("SELECT * FROM wk_records WHERE kind=?", (kind,)):
            payload = json.loads(row["payload"])
            if payload.get("_removed") and not include_removed:
                records.pop(row["id"], None)
                continue
            base = records.get(row["id"], {"id": row["id"], "state": STATES[kind][0],
                                          "archived": False, "notes": "", "project_ids": []})
            base.update(payload)
            base.update(revision=row["revision"], updated_at=row["updated_at"],
                        manual=bool(payload), created_by_user=bool(row["created"]))
            records[row["id"]] = base
        if kind == 'contacts':
            from work_model import enabled, contact_view
            if enabled(conn):
                records = contact_view(self, conn, records)
        return records

    def _record(self, conn, kind, rid):
        item = self._records(conn, kind).get(rid)
        if item is None:
            raise StoreError("记录不存在，或已撤销创建。", 404)
        return item

    def change_token(self):
        files = []
        for path in (self.path, Path(str(self.path) + '-wal')):
            try:
                stat = path.stat()
                files.append((stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                files.append(None)
        return hashlib.sha256(json_text(files).encode()).hexdigest()

    def snapshot(self):
        # Capture before opening the read snapshot: a concurrent write causes a later refresh.
        change_token = self.change_token()
        with self.connect() as conn:
            conn.execute("BEGIN")
            records = {kind: list(self._records(conn, kind).values()) for kind in KINDS}
            stats = {table: conn.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                     for table in ["messages", "documents", "evidence", "facts"]}
            sources = [dict(r) for r in conn.execute(
                "SELECT platform,MAX(collected_at) AS collected_at,COUNT(*) AS snapshots FROM sources GROUP BY platform")]
            raw_summary=None
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='raw_sources'").fetchone():
                from raw_store import schedule_status
                from source_health import in_connection as source_health
                raw_summary={'approved_sources':conn.execute("SELECT count(*) FROM raw_sources WHERE state='approved'").fetchone()[0],
                    'versions':conn.execute('SELECT count(*) FROM raw_versions').fetchone()[0],
                    'pending_changes':conn.execute('SELECT count(*) FROM raw_changes WHERE reviewed_at IS NULL').fetchone()[0],
                    'last_success':conn.execute('SELECT MAX(success_at) FROM raw_sync').fetchone()[0],
                    'schedule':schedule_status(conn),'health':source_health(conn)}
            from people import People
            from completeness import Completeness
            from work_model import enabled
            from relevance import build, score, source_index
            meta = {r['key']: json.loads(r['value']) for r in conn.execute('SELECT * FROM wk_meta')}
            people_index = People(self).index(conn)
            relevance = build(records, meta, people_index)
            for kind, rows in records.items():
                rows.sort(key=lambda record: score(relevance, kind, record['id']))
            redesigned = enabled(conn)
            judgment_items = []
            review_inventory = {}
            if redesigned:
                from judgment_cards import JudgmentCards
                from review_inventory import in_connection
                judgment_items = JudgmentCards(self).queue_in_connection(conn, relevance)
                review_summary = JudgmentCards(self).summary(conn, judgment_items)
                review_inventory = in_connection(self,conn,records,judgment_items,people_index)
            else:
                review_summary = Completeness(self).scan(conn)['summary']
            from work_analysis import WorkAnalysis
            from work_proposals import WorkProposals
            from follow_through import FollowThrough
            work_analysis = WorkAnalysis(self).views(conn, records['tasks'], records['projects'])
            from work_priority import WorkPriority
            from project_priority import ProjectPriority
            review_context = FollowThrough(self).context(conn)
            work_priority = WorkPriority(self).views(conn, records['tasks'], review_context)
            project_priority = ProjectPriority(self).views(conn, records['projects'], review_context)
            return {"records": records, "stats": stats, "sources": sources, "people_index":people_index,
                    "work_priority": work_priority, "project_priority": project_priority,
                    "work_analysis": work_analysis, "work_proposals": WorkProposals(self).rows(conn),
                    "relevance":relevance, "source_relevance":source_index(conn,records,relevance),
                    "review_inventory":review_inventory, "follow_through":FollowThrough(self).status(conn, review_context), "change_token":change_token,
                    "completeness":{} if redesigned else review_summary,
                    "judgments":review_summary if redesigned else {}, "work_model_version":28 if redesigned else 26,
                    "judgment_items":[{k:r[k] for k in ('id','question','state','targets','relevance_rank')} for r in judgment_items if r['state'] in ('pending','deferred')],
                    "states": STATES, "generated_at": now(), "history": self._history(conn, 8), "raw_summary":raw_summary,
                    "taxonomy": {"project_types": PROJECT_TYPES, "org_roles": ORG_ROLES, "task_types": TASK_TYPES, "project_modes": PROJECT_MODES, "department_types": DEPARTMENT_TYPES},
                    "meta": meta,
                    "coverage": ("文字归档、已有语音转写及各来源覆盖时间请看数据来源与更新。图片、文件、视频与音频不另存。"+raw_summary['schedule']['description']) if raw_summary else "个人工作知识库；请先完成来源接入，采集与提醒状态分别核对。"}

    def _evidence(self, conn, ids):
        result = []
        for eid in dict.fromkeys(ids):
            from work_sources import require_conversation, scope
            pointer = conn.execute("SELECT message_id,document_id FROM evidence WHERE id=?", (eid,)).fetchone()
            if not pointer: continue
            try:
                if pointer['message_id']:
                    meta = conn.execute('SELECT platform,conversation_id FROM messages WHERE id=?', (pointer['message_id'],)).fetchone()
                    if not meta: continue
                    require_conversation(meta['platform'], meta['conversation_id'])
                elif pointer['document_id'] not in scope()['documents']:
                    continue
            except ValueError:
                continue
            row = conn.execute("SELECT * FROM evidence WHERE id=?", (eid,)).fetchone()
            if not row:
                continue
            e = dict(row)
            e.pop("locator", None)
            if e["message_id"]:
                m = conn.execute("SELECT sender_name,platform,event_at,url,conversation_name FROM messages WHERE id=?",
                                 (e["message_id"],)).fetchone()
                e.update(dict(m))
            else:
                doc = conn.execute("SELECT title,url,revision,platform FROM documents WHERE id=?",
                                   (e["document_id"],)).fetchone()
                e.update(dict(doc))
                if e["platform"] == "local":
                    e["url"] = ""
            result.append(e)
        return result

    def detail(self, kind, rid):
        with self.connect() as conn:
            conn.execute("BEGIN")
            item = self._record(conn, kind, rid)
            ids, facts, source = [], [], None
            if kind == "projects":
                facts = [dict(r) for r in conn.execute("SELECT * FROM facts WHERE project_id=? ORDER BY id", (rid,))]
                ids = [r[0] for r in conn.execute("SELECT id FROM evidence WHERE project_id=? ORDER BY id", (rid,))]
                for fact in facts:
                    fact["evidence_ids"] = json.loads(fact["evidence_ids"])
            elif kind == "contacts":
                ids = item.get("evidence_ids", [])
            elif kind == "demands":
                ids = [r[0] for r in conn.execute("SELECT evidence_id FROM product_demand_evidence WHERE demand_id=?", (rid,))]
            elif kind in ["capabilities", "roadmap"] and item.get("document_id"):
                source = dict(conn.execute("SELECT id,title,revision,kind FROM documents WHERE id=?", (item["document_id"],)).fetchone())
                source["line"] = item.get("source_line")
            from people import People
            from work_analysis import WorkAnalysis
            from work_lifecycle import WorkLifecycle
            from task_work_links import TaskWorkLinks
            from follow_through import FollowThrough
            return {"record": item, "work_analysis": WorkAnalysis(self).view(conn, item) if kind == 'tasks' else None,
                    "work_priority": __import__('work_priority').WorkPriority(self).view(conn, item) if kind == 'tasks' else None,
                    "work_priority_history": __import__('work_priority').WorkPriority(self).history(conn, rid) if kind == 'tasks' else [],
                    "project_priority": __import__('project_priority').ProjectPriority(self).view(conn, item) if kind == 'projects' else None,
                    "project_priority_history": __import__('project_priority').ProjectPriority(self).history(conn, rid) if kind == 'projects' else [],
                    "follow_through":FollowThrough(self).detail(conn,kind,rid),
                    "work_evidence_reviews": WorkLifecycle(self).history(conn, rid) if kind == 'tasks' else [],
                    "work_goal": __import__('work_completion').WorkCompletion.goal(conn, rid) if kind == 'tasks' else None,
                    "external_task_links": TaskWorkLinks(self).for_work(conn, rid) if kind == 'tasks' else [],
                    "facts": facts, "evidence": self._evidence(conn, ids),
                    "dossier": People(self).dossier(conn,rid) if kind=='people' else None,
                    "person_roles":People(self).relations(conn,kind=kind,target_id=rid) if kind in ('projects','organizations','departments') else [],
                    "source": source, "related": self._related(conn, kind, rid), "history": self._history(conn, 30, kind, rid), "fields": FIELDS[kind], "merged_history": self._merged_history(conn, rid) if kind == "projects" else []}

    def _merged_history(self, conn, rid):
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='wk_project_merges'").fetchone():
            return []
        return [json.loads(row[0]) for row in conn.execute('SELECT snapshot FROM wk_project_merges WHERE project_id=? ORDER BY source_id', (rid,))]

    def _related(self, conn, kind, rid):
        result = []
        item = self._record(conn, kind, rid)
        for field, (target, multiple) in RELATIONS.items():
            if field not in FIELDS[kind]: continue
            ids = item.get(field, []) if multiple else [item.get(field)]
            for target_id in ids or []:
                record = self._records(conn, target).get(target_id)
                if record:
                    result.append(dict(kind=target, id=target_id, name=record.get('name', record.get('title', target_id)), relation=field, direction='out', archived=record.get('archived', False)))
        for source_kind in KINDS:
            for field, (target, multiple) in RELATIONS.items():
                if target != kind or field not in FIELDS[source_kind]: continue
                for record in self._records(conn, source_kind).values():
                    ids = record.get(field, []) if multiple else [record.get(field)]
                    if rid in (ids or []):
                        result.append(dict(kind=source_kind, id=record['id'], name=record.get('name', record.get('title', record['id'])), relation=field, direction='in', archived=record.get('archived', False)))
        return result

    def _check_hierarchy(self, conn, kind, rid, clean):
        if kind == 'people' and 'accounts' in clean:
            from work_model import check_account_ownership
            check_account_ownership(self, conn, rid, clean['accounts'])
        field = 'manager_id' if kind == 'people' else 'parent_org_id' if kind == 'organizations' else 'parent_department_id' if kind == 'departments' else None
        if field and field in clean:
            seen, cursor = {rid}, clean[field]
            records = self._records(conn, kind)
            while cursor:
                if cursor in seen: raise StoreError('上级关系不能形成循环。')
                seen.add(cursor)
                cursor = records[cursor].get(field, '')
        if kind=='departments':
            records=self._records(conn,kind);current=dict(records.get(rid,{}),**clean)
            parent=records.get(current.get('parent_department_id'))
            if parent and parent.get('organization_id')!=current.get('organization_id'):
                raise StoreError('上级部门必须属于同一组织。')
            if any(d.get('parent_department_id')==rid and d.get('organization_id')!=current.get('organization_id') for d in records.values()):
                raise StoreError('部门仍有下级部门，所属组织必须保持一致。')
            from people import People
            if People.exists(conn) and conn.execute("SELECT 1 FROM wk_person_relations WHERE department_id=? AND represented_org_id<>'' AND represented_org_id<>? AND state<>'rejected' LIMIT 1",(rid,current.get('organization_id',''))).fetchone():
                raise StoreError('现有人员关系代表该部门所属组织，请先调整这些关系再更改部门所属组织。')

    def document(self, docid):
        from work_sources import read_document
        with self.connect() as conn:
            try:
                return read_document(conn, docid)
            except ValueError as exc:
                raise StoreError(str(exc), 403) from None

    def _validate(self, conn, kind, data, creation=False):
        if not isinstance(data, dict) or not data:
            raise StoreError("请提供要保存的内容。")
        if set(data) - set(FIELDS[kind]):
            raise StoreError("包含不允许修改的内容；原始来源和证据只读。")
        clean = {}
        for key, value in data.items():
            if key == 'accounts' and kind == 'people':
                from work_model import validate_accounts
                value = validate_accounts(self, conn, value)
            elif key in ('work_log', 'context_history'):
                from work_model import validate_log
                value = validate_log(value)
            elif key == "archived":
                if type(value) is not bool:
                    raise StoreError("归档状态格式不正确。")
            elif key in RELATIONS:
                target, multiple = RELATIONS[key]
                if multiple:
                    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
                        raise StoreError("关联格式不正确。")
                    value = list(dict.fromkeys(value))
                    ids = value
                else:
                    if not isinstance(value, str): raise StoreError("关联格式不正确。")
                    value = value.strip()
                    ids = [value] if value else []
                if any(v not in self._records(conn, target) for v in ids):
                    raise StoreError("关联的记录不存在。")
            elif key in ('roles', 'work_types'):
                options = ORG_ROLES if key == 'roles' else PROJECT_TYPES
                if not isinstance(value, list) or any(v not in options for v in value):
                    raise StoreError("请选择有效类别。")
                value = list(dict.fromkeys(value))
            else:
                if not isinstance(value, str):
                    raise StoreError("请填写文本内容。")
                value = value.strip()
                if len(value) > (12000 if kind == "projects" or key in ["notes", "description", "boundary", "stage", "next_action", "progress", "result", "background", "schedule_note", "dependencies"] else 500):
                    raise StoreError("内容过长，请精简后保存。")
                if key in ["target_date", "due_date", "source_date"] and value:
                    try:
                        dt.date.fromisoformat(value)
                    except ValueError:
                        raise StoreError("日期格式应为年-月-日。") from None
                if key == "state" and value not in STATES[kind]:
                    raise StoreError("请选择有效状态。")
                if key == 'primary_type' and value not in ['', *PROJECT_TYPES]:
                    raise StoreError('请选择有效项目类型。')
                if key == 'task_type' and value not in ['', *TASK_TYPES]:
                    raise StoreError('请选择有效待办类型。')
                if key == 'support_mode' and value not in ['', *PROJECT_MODES]:
                    raise StoreError('请选择有效支持方式。')
                if key == 'unit_type' and value not in DEPARTMENT_TYPES:
                    raise StoreError('请选择有效部门类型。')
                if key == 'decision' and value not in ['', '同意建议', '调整后推进', '暂缓', '不做']:
                    raise StoreError('请选择有效处理决定。')
                if key == "priority" and value not in (["", "高", "普通", "低"] if kind in ('tasks', 'projects') else ["", "高", "中", "低"]):
                    raise StoreError("请选择有效优先级。")
                if key in ["project_id", "capability_id"] and value:
                    target = "projects" if key == "project_id" else "capabilities"
                    if value not in self._records(conn, target):
                        raise StoreError("关联的项目或产品能力不存在。")
            clean[key] = value
        required = REQUIRED.get(kind)
        if required and ((creation and not clean.get(required)) or required in clean and not clean[required]):
            raise StoreError("名称或事项不能为空。")
        if kind=='departments' and ((creation and not clean.get('organization_id')) or 'organization_id' in clean and not clean['organization_id']):
            raise StoreError('部门必须归属一个组织。')
        return clean

    def _write(self, conn, kind, rid, payload, before, revision, action, actor, reason, created=False, undo_of=None):
        from work_model import enabled
        if kind in ('contacts', 'updates') and enabled(conn):
            raise StoreError('账号已并入人员，进展已并入工作；请使用对应档案维护入口。', 409)
        if kind == 'demands' and enabled(conn):
            raise StoreError('产品需求本阶段只读保留，后续另行讨论。', 409)
        stamp = now()
        base = self._base(conn, kind).get(rid, {"state": STATES[kind][0], "archived": False})
        def display(overlay):
            if overlay.get("_removed"):
                return {"_removed": True}
            effective = dict(base)
            effective.update(overlay)
            result = {key: effective.get(key, "") for key in set(FIELDS[kind]) | {"name", "title"}}
            if kind == 'tasks' and effective.get('_work_analysis'):
                analysis = effective['_work_analysis']
                result['_analysis_summary'] = {'当前情况': analysis['current'], '下一步（含时间）': analysis['next_step'],
                                               '处理': analysis.get('decision', {}).get('state', 'pending')}
            return result
        conn.execute("""INSERT INTO wk_records VALUES (?,?,?,?,?,?)
            ON CONFLICT(kind,id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision,updated_at=excluded.updated_at""",
                     (kind, rid, json_text(payload), revision + 1, stamp, int(created)))
        conn.execute("""INSERT INTO wk_changes(kind,record_id,action,before_payload,after_payload,
            before_revision,after_revision,happened_at,actor,reason,undo_of,before_view,after_view) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (kind, rid, action, json_text(before), json_text(payload), revision, revision + 1,
                      stamp, str(actor)[:100], str(reason)[:1000], undo_of, json_text(display(before)), json_text(display(payload))))
        change_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        if kind in ('projects', 'tasks') and action != '判断确认':
            from review_requirements import reconcile_in_transaction
            reconcile_in_transaction(self, conn, {(kind, rid)})
        return {"id": rid, "revision": revision + 1, "updated_at": stamp,
                "change_id": change_id}

    def create(self, kind, data, actor="本机工作台", reason=""):
        self._kind(kind)
        if kind == "capabilities":
            raise StoreError("功能目录由产品统一资料导入；可以维护核对状态和备注。")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            clean = self._validate(conn, kind, data, True)
            clean.setdefault("state", STATES[kind][0])
            clean['_provenance'] = {key: dict(mode='user', actor=actor, at=now(), reason=reason, sources=[])
                                    for key,value in clean.items() if value not in ('', [], None)}
            rid = "manual-" + uuid.uuid4().hex[:16]
            self._check_hierarchy(conn,kind,rid,clean)
            return self._write(conn, kind, rid, clean, {"_removed": True}, 0, "新建", actor, reason, True)

    def update(self, kind, rid, data, expected_revision, actor="本机工作台", reason=""):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return self.update_in_transaction(conn, kind, rid, data, expected_revision, actor, reason)

    def update_in_transaction(self, conn, kind, rid, data, expected_revision, actor="本机工作台", reason="", action_override=None):
        """Same validation/audit inside a caller-owned atomic batch."""
        self._kind(kind)
        if type(expected_revision) is not int or expected_revision < 0:
            raise StoreError("缺少当前记录版本，请重新打开记录。")
        current = self._record(conn, kind, rid)
        if current["revision"] != expected_revision:
            raise StoreError("这条记录已被其他操作更新。你的输入仍保留，请重新读取后合并。", 409)
        clean = self._validate(conn, kind, data)
        for key in ('work_log', 'context_history'):
            if key in clean:
                previous = current.get(key) or []
                if clean[key][:len(previous)] != previous:
                    raise StoreError('处理过程保留历史；请追加更正说明，不能覆盖已有过程。')
                for appended in clean[key][len(previous):]:
                    if appended.get('mode') in ('fact', 'inference'):
                        from people import People
                        appended['sources'] = People._refs(conn, appended.get('sources'))
                    elif appended.get('mode') == 'migration':
                        raise StoreError('普通维护不能创建结构迁移记录，请追加本次实际说明。')
        self._check_hierarchy(conn, kind, rid, clean)
        prior = conn.execute("SELECT * FROM wk_records WHERE kind=? AND id=?", (kind, rid)).fetchone()
        before = json.loads(prior["payload"]) if prior else {}
        after = dict(before)
        changed = {key: value for key, value in clean.items() if current.get(key, "") != value}
        # An explicit priority choice is a user decision even if the old
        # evidence-derived label happens to have the same text. UI forms omit
        # untouched priority controls so unrelated edits never pin a label.
        if kind in ('tasks', 'projects') and 'priority' in clean and current.get('_provenance', {}).get('priority', {}).get('mode') != 'user':
            changed['priority'] = clean['priority']
        after.update(changed)
        if changed:
            provenance = dict(before.get('_provenance', {}))
            for key in changed:
                prior_source = provenance.get(key, {})
                provenance[key] = dict(mode='user', actor=actor, at=now(), reason=reason,
                                       sources=prior_source.get('sources', []), previous_mode=prior_source.get('mode'))
            after['_provenance'] = provenance
        if not changed:
            return {"id": rid, "revision": expected_revision, "unchanged": True}
        action = "归档" if clean.get("archived") is True else "恢复" if clean.get("archived") is False and current.get("archived") else "修改"
        return self._write(conn, kind, rid, after, before, expected_revision, action_override or action, actor, reason,
                           bool(prior and prior["created"]))

    def undo(self, change_id, actor="本机工作台"):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            change = conn.execute("SELECT * FROM wk_changes WHERE id=?", (change_id,)).fetchone()
            if not change:
                raise StoreError("修改记录不存在。", 404)
            if change['kind'] == 'work_preferences':
                raise StoreError('请在工作页的关注人设置中调整名单；每次调整均保留历史。', 409)
            if change['action'] in ('合并人员','撤销人员合并'):
                raise StoreError('请在人员集中核对页的已处理记录中撤销整次合并，不能单独撤销其中一个字段。', 409)
            if change['action'] == '判断确认':
                raise StoreError('请在待你判断的已处理记录中重新打开问题，再确认新的决定；不能单独撤销其中一项写回。', 409)
            if change['kind'] not in KINDS or change['action'] in ('合并项目', '工作结构迁移', '人员账号迁移', '进展归入工作', '历史事项归并'):
                raise StoreError('这是已完成的结构迁移历史，不能单条撤销；迁移前完整备份已保留。', 409)
            current = conn.execute("SELECT * FROM wk_records WHERE kind=? AND id=?", (change["kind"], change["record_id"])).fetchone()
            if not current or current["revision"] != change["after_revision"] or current["payload"] != change["after_payload"]:
                raise StoreError("这次修改之后已有新操作，不能直接撤销覆盖；请查看当前内容。", 409)
            prior = json.loads(change['before_payload'])
            if prior.get('_removed') and any(r['direction'] == 'in' for r in self._related(conn, change['kind'], change['record_id'])):
                raise StoreError('这条记录已被其他记录关联，请先解除关联再撤销创建。', 409)
            if prior.get('_removed'):
                from people import People
                if People.exists(conn):
                    kind,rid=change['kind'],change['record_id']
                    referenced=conn.execute("SELECT 1 FROM wk_person_relations WHERE (kind=? AND target_id=?) OR (?='people' AND person_id=?) OR (?='organizations' AND represented_org_id=?) OR (?='departments' AND department_id=?) LIMIT 1",(kind,rid,kind,rid,kind,rid,kind,rid)).fetchone()
                    if referenced:raise StoreError('人员关系仍引用这条记录，请使用归档保留历史，不能撤销创建。',409)
            self._check_hierarchy(conn, change['kind'], change['record_id'], prior)
            return self._write(conn, change["kind"], change["record_id"], json.loads(change["before_payload"]),
                               json.loads(change["after_payload"]), current["revision"], "撤销", actor,
                               "撤销修改 #" + str(change_id), bool(current["created"]), change_id)

    def _history(self, conn, limit=100, kind=None, rid=None):
        query = """SELECT c.*,r.revision AS current_revision FROM wk_changes c
            LEFT JOIN wk_records r ON c.kind=r.kind AND c.record_id=r.id"""
        args = []
        if kind and rid:
            query += " WHERE c.kind=? AND c.record_id=?"
            args = [kind, rid]
        rows = conn.execute(query + " ORDER BY c.id DESC LIMIT ?", [*args, limit]).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            raw_before = json.loads(item.pop("before_payload"))
            raw_after = json.loads(item.pop("after_payload"))
            item["before"] = json.loads(item.pop("before_view")) or raw_before
            item["after"] = json.loads(item.pop("after_view")) or raw_after
            item["can_undo"] = item.pop("current_revision") == item["after_revision"] and item["kind"] in KINDS and item["action"] not in ("合并项目", "合并人员", "撤销人员合并", "工作结构迁移", "人员账号迁移", "进展归入工作", "历史事项归并", "判断确认")
            item["changed_fields"] = [k for k in set(item["before"]) | set(item["after"])
                                      if item["before"].get(k) != item["after"].get(k)]
            result.append(item)
        return result

    def history(self):
        with self.connect() as conn:
            return self._history(conn)

    def export(self):
        data = self.snapshot()
        data["history"] = self.history()
        with self.connect() as conn:
            from people import People
            data['person_relations'] = People(self).relations(conn)
            data['person_events'] = [dict(r) for r in conn.execute('SELECT * FROM wk_person_events ORDER BY id')] if People.exists(conn) else []
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='wk_identity_reviews'").fetchone():
                data['identity_reviews']=[{k:v for k,v in dict(r).items() if k!='merge_backup'} for r in conn.execute('SELECT * FROM wk_identity_reviews')]
                data['review_notes']=[dict(r) for r in conn.execute('SELECT * FROM wk_review_notes')]
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='wk_completeness_answers'").fetchone():
                data['completeness_answers']=[dict(r) for r in conn.execute('SELECT * FROM wk_completeness_answers')]
                data['completeness_events']=[dict(r) for r in conn.execute('SELECT * FROM wk_completeness_events ORDER BY id')]
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='wk_judgment_cards'").fetchone():
                data['judgment_cards']=[dict(r) for r in conn.execute('SELECT * FROM wk_judgment_cards')]
                data['judgment_events']=[dict(r) for r in conn.execute('SELECT * FROM wk_judgment_events ORDER BY id')]
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='wk_work_evidence_reviews'").fetchone():
                data['work_evidence_reviews']=[dict(r) for r in conn.execute('SELECT * FROM wk_work_evidence_reviews ORDER BY reviewed_at,version_id')]
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='wk_work_goals'").fetchone():
                data['work_goals']=[dict(r) for r in conn.execute('SELECT * FROM wk_work_goals ORDER BY work_id')]
                data['work_goal_events']=[dict(r) for r in conn.execute('SELECT * FROM wk_work_goal_events ORDER BY id')]
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='wk_work_priority_reviews'").fetchone():
                data['work_priority_reviews']=[dict(r) for r in conn.execute('SELECT * FROM wk_work_priority_reviews ORDER BY work_id')]
                data['work_priority_events']=[dict(r) for r in conn.execute('SELECT * FROM wk_work_priority_events ORDER BY id')]
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='wk_project_priority_reviews'").fetchone():
                data['project_priority_reviews']=[dict(r) for r in conn.execute('SELECT * FROM wk_project_priority_reviews ORDER BY project_id')]
                data['project_priority_events']=[dict(r) for r in conn.execute('SELECT * FROM wk_project_priority_events ORDER BY id')]
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='wk_followthrough_reviews'").fetchone():
                data['follow_through_reviews']=[dict(r) for r in conn.execute('SELECT * FROM wk_followthrough_reviews ORDER BY kind,record_id')]
                data['follow_through_events']=[dict(r) for r in conn.execute('SELECT * FROM wk_followthrough_events ORDER BY id')]
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='wk_task_work_links'").fetchone():
                data['task_work_links']=[dict(r) for r in conn.execute('SELECT * FROM wk_task_work_links ORDER BY source_id')]
                data['task_link_events']=[dict(r) for r in conn.execute('SELECT * FROM wk_task_link_events ORDER BY id')]
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name='wk_model_migration_records'").fetchone():
                data['work_model_migration']=[dict(r) for r in conn.execute('SELECT * FROM wk_model_migration_records ORDER BY kind,id')]
            data['merged_history'] = {r['id']: self._merged_history(conn, r['id']) for r in data['records']['projects']}
        data["description"] = "维护后的项目、人员与账号、工作及判断视图，包含迁移历史与业务依据摘录；不含原始消息全集或文档全文，也不替代数据库备份。"
        return data

    def baseline_hashes(self):
        with self.connect() as conn:
            tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'wk_%' AND name NOT LIKE 'sqlite_%'")]
            return {t: hashlib.sha256(json_text([tuple(r) for r in conn.execute('SELECT * FROM "' + t + '" ORDER BY rowid')]).encode()).hexdigest() for t in tables}
