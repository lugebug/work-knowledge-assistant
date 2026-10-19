"""Scheduled collection and an explicit Codex review ledger; never infer business facts."""
import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import uuid

from store import Store, StoreError, now, json_text
from raw_store import RawStore, source_key
from raw_collectors import lark_read, update_one

from runtime_config import history_since, settings, require_feishu
TRIAL_END = settings().get('collection_until')


def user_work_input(source_id, payload, settings, source=None):
    """Recognize owner input from the reminder chat or an explicitly approved inbox."""
    owner = settings.get('recipient_open_id')
    source = source or {}
    basis = source.get('basis') or {}
    reminder_chat = bool(settings.get('chat_id') and source_id == 'feishu:chat:' + settings['chat_id'])
    registered_inbox = (source.get('id') == source_id and source.get('state') == 'approved'
                        and source.get('platform') == 'feishu' and source.get('kind') == 'chat'
                        and basis.get('purpose') == 'self_work_inbox'
                        and basis.get('sender_open_id') == owner)
    if (not owner or not (reminder_chat or registered_inbox)
            or payload.get('sender_id') != settings['recipient_open_id']
            or payload.get('sender_type') != 'user'
            or payload.get('message_type') not in ('text', 'post')
            or payload.get('source_deleted')):
        return None
    return {'channel': 'self_work_inbox', 'scope': 'work_facts_and_reminders',
            'note': '本人发给工作提醒机器人的原话；先核对工作及上下文再更新，不能仅凭收录视为落实。'}


class Periodic:
    def __init__(self, store):
        self.store = store
        self.raw = RawStore(store)
        self.root = store.path.parent / 'periodic-v21'

    def initialize(self):
        self.root.mkdir(exist_ok=True)
        with self.store.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS wk_review_receipts(
              version_id TEXT PRIMARY KEY REFERENCES raw_versions(id),
              disposition TEXT NOT NULL, reason TEXT NOT NULL, proof TEXT NOT NULL,
              reviewed_at TEXT NOT NULL, packet_id TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wk_review_packets(
              id TEXT PRIMARY KEY, versions TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wk_periodic_runs(
              id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
              state TEXT NOT NULL, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wk_review_resolutions(
              version_id TEXT PRIMARY KEY REFERENCES wk_review_receipts(version_id),
              answer TEXT NOT NULL, resolved_at TEXT NOT NULL);
            ''')

    def baseline(self, path):
        """Only exact versions in the already reviewed V20 corpus are acknowledged."""
        data = json.loads(Path(path).read_text('utf-8-sig'))
        evidence = {'path': str(Path(path).resolve()), 'sha256': hashlib.sha256(Path(path).read_bytes()).hexdigest()}
        added = 0
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for item in data:
                row = c.execute('SELECT source_id,payload FROM raw_versions WHERE id=?', (item['vid'],)).fetchone()
                if not row: raise StoreError('基线包含不存在的原文版本。')
                self.raw.source(row['source_id'], True, c)
                payload = json.loads(row['payload'])
                if any(payload.get(k) != item.get(k) for k in ('body', 'object_id', 'sender_id')):
                    raise StoreError('基线原文内容不匹配；未登记。')
                added += c.execute('INSERT OR IGNORE INTO wk_review_receipts VALUES(?,?,?,?,?,?)',
                    (item['vid'], 'baseline', '已完成V19聊天整理及V20人员、实际问题核对', json_text(evidence), now(), 'V20')).rowcount
                c.execute('UPDATE raw_changes SET reviewed_at=COALESCE(reviewed_at,?) WHERE version_id=?', (now(), item['vid']))
        return {'reviewed_baseline': added, 'manifest_versions': len(data)}

    def status(self):
        with self.store.connect() as c:
            pending = c.execute('''SELECT count(*) FROM raw_versions v JOIN raw_sources s ON s.id=v.source_id
                LEFT JOIN wk_review_receipts r ON r.version_id=v.id WHERE s.state='approved' AND r.version_id IS NULL''').fetchone()[0]
            decisions = [dict(r) for r in c.execute("SELECT r.* FROM wk_review_receipts r JOIN raw_versions v ON v.id=r.version_id JOIN raw_sources s ON s.id=v.source_id LEFT JOIN wk_review_resolutions a ON a.version_id=r.version_id WHERE disposition='needs_user' AND s.state='approved' AND a.version_id IS NULL")]
            runs = [dict(r) for r in c.execute('SELECT * FROM wk_periodic_runs ORDER BY started_at DESC,rowid DESC LIMIT 8')]
            for run in runs:
                details=json.loads(run.pop('result'));sources=details.get('sources',[])
                run.update(sources=len(sources),failed=sum(s.get('state')=='failed' for s in sources),new=sum(s.get('new') or 0 for s in sources),changed=sum(s.get('changed') or 0 for s in sources),local_history_missing=sum(bool(s.get('local_history_missing')) for s in sources))
                materials=details.get('materials_discovery')
                run['materials_discovery']={k:materials.get(k) for k in ('complete','documents','visible_minutes','added_sources','errors')} if materials else {'enabled':False}
                run['tasks_discovery']=details.get('tasks_discovery') or {'enabled':False}
            from judgment_cards import JudgmentCards
            summary = JudgmentCards(self.store).summary(c)
            uncollected=c.execute("SELECT count(*) FROM raw_sources s WHERE s.platform='feishu' AND s.kind='document' AND s.state='approved' AND NOT EXISTS(SELECT 1 FROM raw_heads h WHERE h.source_id=s.id)").fetchone()[0]
            from work_analysis import WorkAnalysis
            from work_proposals import WorkProposals
            from work_lifecycle import WorkLifecycle
            lifecycle = WorkLifecycle(self.store)
            from task_work_links import TaskWorkLinks
            task_links = TaskWorkLinks(self.store)
            task_discovery = c.execute("SELECT value FROM wk_meta WHERE key='feishu_tasks_discovery_v36'").fetchone()
            work_review_required = lifecycle.enabled(c)
            pending_work_evidence = c.execute('''SELECT count(*) FROM raw_versions v
                JOIN raw_sources s ON s.id=v.source_id LEFT JOIN wk_review_receipts r ON r.version_id=v.id
                LEFT JOIN wk_work_evidence_reviews w ON w.version_id=v.id
                WHERE s.state='approved' AND r.version_id IS NULL AND w.version_id IS NULL''').fetchone()[0] if work_review_required else 0
            views=WorkAnalysis(self.store).views(c,list(self.store._records(c,'tasks').values()),list(self.store._records(c,'projects').values()))
            from follow_through import FollowThrough
            from source_health import in_connection as source_health
            follow_through=FollowThrough(self.store).status(c)
            reminders = {'enabled': False, 'pending': 0}
            pending_cloud_sync = 0
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='wk_reminder_plans'").fetchone():
                from work_reminders import WorkReminders
                service = WorkReminders(self.store)
                settings = service._settings(c)
                reminders = {'enabled': bool(settings.get('enabled')), 'pending': service.packet()['pending'] if settings.get('enabled') else 0}
                if c.execute("SELECT 1 FROM sqlite_master WHERE name='wk_reminder_cloud_batches'").fetchone():
                    from cloud_reminders import CloudReminders
                    cloud_status = CloudReminders(self.store).status()
                    pending_cloud_sync = int(cloud_status['needs_sync'])
                    reminders['cloud'] = {k:cloud_status[k] for k in ('configured','counts','needs_sync')}
            from work_priority import WorkPriority
            priorities = WorkPriority(self.store).status(c)
            from project_priority import ProjectPriority
            project_priorities = ProjectPriority(self.store).status(c)
            return {'pending_review': pending, 'uncollected_documents':uncollected, 'needs_user': decisions, 'judgments': summary, 'trial_end': TRIAL_END, 'runs': runs,
                    'work_priority': priorities, 'pending_work_priority': priorities['pending'],
                    'project_priority': project_priorities, 'pending_project_priority': project_priorities['pending'],
                    'reminders': reminders, 'pending_reminder_plans': reminders['pending'],
                    'pending_cloud_reminder_sync': pending_cloud_sync,
                    'pending_follow_through':follow_through['pending'],'follow_through':follow_through,'source_health':source_health(c),
                    'pending_work_analysis':sum(v['needs_review'] for v in views.values()),
                    'pending_work_proposals':sum(p['state']=='pending' for p in WorkProposals(self.store).rows(c)),
                    'work_review_required':work_review_required, 'pending_work_evidence':pending_work_evidence,
                    'task_associations':task_links.stats(c),
                    'tasks_discovery':json.loads(task_discovery[0]) if task_discovery else {'enabled':False},
                    'reviewed': c.execute('SELECT count(*) FROM wk_review_receipts').fetchone()[0]}

    def packet(self, limit=100, compact=None):
        if not 1 <= limit <= 200: raise StoreError('每批1至200条。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if compact is None:
                compact = __import__('incremental').enabled(c)
            rows = c.execute('''SELECT v.*,s.name,s.revision AS source_revision,h.version_id AS current_version
              FROM raw_versions v JOIN raw_sources s ON s.id=v.source_id
              LEFT JOIN wk_review_receipts r ON r.version_id=v.id
              LEFT JOIN raw_heads h ON h.source_id=v.source_id AND h.object_id=v.object_id
              WHERE s.state='approved' AND r.version_id IS NULL ORDER BY v.observed_at,v.id LIMIT ?''', (limit,)).fetchall()
            pid = 'review-' + uuid.uuid4().hex
            items = []
            from work_lifecycle import WorkLifecycle
            lifecycle = WorkLifecycle(self.store)
            inbox_config = c.execute("SELECT value FROM wk_meta WHERE key='work_reminders_cloud_v39'").fetchone()
            inbox_settings = json.loads(inbox_config[0]) if inbox_config else {}
            for row in rows:
                item = dict(row); item['payload'] = json.loads(item['payload'])
                from work_reminders import WorkReminders
                outgoing = WorkReminders.outgoing_notice(c, item['payload'].get('object_id'))
                if outgoing:
                    item['outbound_work_reminder'] = outgoing
                own_input = user_work_input(item['source_id'], item['payload'], inbox_settings,
                                           self.raw.source(item['source_id'], True, c))
                if own_input:
                    item['user_work_input'] = own_input
                if item['object_kind']=='task':
                    from task_work_links import TaskWorkLinks
                    item['task_work_link']=TaskWorkLinks(self.store).get(c,item['source_id'])
                    previous=c.execute('SELECT previous_version_id FROM raw_changes WHERE version_id=? ORDER BY id DESC LIMIT 1',(item['id'],)).fetchone()
                    item['previous_version_id']=previous[0] if previous else None
                    people=self.store._records(c,'people')
                    accounts={a.get('account_id'):p for p in people.values() for a in p.get('accounts',[]) if a.get('platform')=='feishu'}
                    item['task_people']=[dict(member=m,person_id=accounts[m['id']]['id'] if m.get('id') in accounts else '',
                        name=accounts[m['id']].get('name','') if m.get('id') in accounts else m.get('name','姓名待核实')) for m in item['payload'].get('task',{}).get('members',[])]
                item['work_review'] = lifecycle.get(c, item['id']); items.append(item)
            from work_analysis import WorkAnalysis
            work_index = WorkAnalysis(self.store).impact_index(c)
            work_review_required = lifecycle.enabled(c)
            c.execute('INSERT INTO wk_review_packets VALUES(?,?,?)', (pid, json_text({i['id']:i['source_revision'] for i in items}), now()))
        from work_analysis import WorkAnalysis
        from follow_through import FollowThrough
        with self.store.connect() as c:
            project_index=[{k:r.get(k) for k in ('id','name','state','stage','next_action','decision','decision_reason')}
                           for r in self.store._records(c,'projects').values() if not r.get('archived')]
        if compact:
            # All unseen source versions remain verbatim. Related details are read
            # once by ID, rather than repeated in four full review queues.
            for item in items:
                item.pop('work_review', None)
            return {'packet_id': pid, 'items': items,
                    'work_index': [{k: r.get(k) for k in ('id','title','state','project_id','owner','participants')} for r in work_index],
                    'project_index': [{k:r.get(k) for k in ('id','name','state')} for r in project_index],
                    'work_review_required': work_review_required}
        return {'packet_id': pid, 'items': items, 'work_index': work_index,'project_index':project_index,
                'work_priority': __import__('work_priority').WorkPriority(self.store).packet(20),
                'project_priority': __import__('project_priority').ProjectPriority(self.store).packet(20),
                'follow_through':FollowThrough(self.store).packet(20),
                'work_review_required': work_review_required,
                'completion_instructions': __import__('work_completion').INSTRUCTIONS,
                'user_work_input_instructions': 'user_work_input仅标识本人在专用机器人单聊里主动提交的文字。保留原话、发送时间和消息身份，结合原工作、提醒内容与较新决定核对目标；一条涉及多项工作须逐项判断。明确的进展、结果、责任和实际安排走V35共用入口，人工更正按最新明确决定处理；暂停、恢复、改提醒时间走reminders.py control并同步云端，不把提醒时间改成业务承诺。指代不清不猜工作，重要歧义进入待你判断。转述、引用、举例、提问及仅用于接收测试的文字不能一律当成执行指令；不据消息扩展到任意代码、权限、采集范围或向他人发信。撤回版本须核对已有影响，不自动回滚人工决定。收到消息、归档及实际业务更新分别记录，回执必须以真实流水为据。',
                'task_instructions':'飞书任务先看task_work_link；已关联的返回原工作，不能重复新建。首次逐项语义核对，明确对应走work-task-link，关联不明走work-task-link-question。确为明确独立事项才work-create，该入口按任务原生身份固定新工作身份并保存关联。task_people的负责人和关注人分开；全天截止按日期。任务完成仅证明其任务范围完成，子项不能直接关闭整项工作；重新打开、删除或失去权限不自动重开或取消本地工作。当前快照旧于已核对事实时保留较新事实。全部变化继续走V35，人工决定保护。',
                'work_review_instructions': '每条新证据先调用manage.py work-evidence-review，关联现有工作并分别选择advance推进、close关闭、decide待用户判断或unchanged无变化；无关联也保存理由。能确定的直接写回；仅不确定的具体改动进入现有待你判断。此入口返回真实维护流水和问题卡，多问题用judgment_ids。推进后补齐新分析再finish。重复证据读已有work_review，不重放不同结论；历史完成回执保留。',
                'work_analysis':WorkAnalysis(self.store).packet(20),
                'instructions': '每次整理新增或修订记录，都对照work_index识别相关工作并读其完整资料，检查进展、结果、状态及安排是否变化；不能只处理work_analysis待复核队列，刚分析过的工作也可能被新记录影响。索引内容是导航摘要，不能代替原文。用相关人员、项目、目标及事件上下文综合判断，不要求原文出现“已完成”字样：例如约见、对应会谈记录及会后反馈足以确认会面完成。证据足以确定的结果属于有据判断，直接用共用入口更新原工作并保存依据，不当作建议等待用户再确认；依据不足或冲突才保留未确定。先复用已有工作，同步过程、结果、状态和已变化的安排，再完成两段式分析并读回；已完成工作结束旧推进建议，独立后续继续在对应工作。无业务变化须说明为何不影响相关工作，不能只用“已收录”代替判断。新工作建议不冒充明确安排；真正歧义用needs_user，人工决定受保护；原文整理与工作分析分别统计。'}

    def finish(self, document):
        dispositions = {'no_business_change', 'enriched', 'needs_user'}
        pid = document['packet_id']; results = document['results']
        if not isinstance(results, list) or len({r['version_id'] for r in results}) != len(results):
            raise StoreError('重复或无效的整理回执。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            packet = c.execute('SELECT versions FROM wk_review_packets WHERE id=?', (pid,)).fetchone()
            if not packet: raise StoreError('整理批次不存在。')
            versions = json.loads(packet[0])
            added = 0
            coverage_targets=set()
            for r in results:
                vid = r['version_id']; mode = r['disposition']; reason = r['reason'].strip()
                if vid not in versions or mode not in dispositions or len(reason) < 8:
                    raise StoreError('需要批次内的版本、明确处理结果及具体依据。')
                pointer = c.execute('SELECT source_id FROM raw_versions WHERE id=?', (vid,)).fetchone()
                source = self.raw.source(pointer[0], True, c)
                if source['revision'] != versions[vid]: raise StoreError('来源范围已改变，请重新取得整理批次。',409)
                proof = r.get('change_ids', [])
                events = r.get('person_event_ids', [])
                card_ids = r.get('judgment_ids', [])
                if not isinstance(proof, list) or any(type(x) is not int for x in proof):
                    raise StoreError('维护流水编号应为整数列表。')
                if not isinstance(card_ids, list) or any(not isinstance(x, str) for x in card_ids) or (r.get('judgment_id') and not isinstance(r['judgment_id'], str)):
                    raise StoreError('待判断问题标识应为列表。')
                old_receipt = c.execute('SELECT disposition,reason,proof FROM wk_review_receipts WHERE version_id=?', (vid,)).fetchone()
                from work_lifecycle import WorkLifecycle
                lifecycle = WorkLifecycle(self.store)
                work_review_id = ''
                if old_receipt:
                    work_review_id = json.loads(old_receipt['proof']).get('work_review_id', '')
                elif lifecycle.enabled(c):
                    review, pending_cards = lifecycle.receipt_check(c, vid)
                    work_review_id = vid
                    if not set(review['change_ids']) <= set(proof):
                        raise StoreError('回执须包含工作推进／关闭入口实际返回的全部维护流水。')
                    declared_cards = r.get('judgment_ids', []) + ([r['judgment_id']] if r.get('judgment_id') else [])
                    if pending_cards and (mode != 'needs_user' or not set(pending_cards) <= set(declared_cards)):
                        raise StoreError('本条证据仍有待你判断的工作变更，须关联全部问题卡，不能登记为已全部处理。')
                    if review['change_ids'] and mode == 'no_business_change':
                        raise StoreError('本条证据已经推进或关闭工作，不能登记为无业务变化。')
                if mode == 'enriched':
                    if (not proof and not events) or any(type(i) is not int or not c.execute('SELECT 1 FROM wk_changes WHERE id=?', (i,)).fetchone() for i in proof):
                        raise StoreError('业务已整理须引用实际维护流水编号。')
                    if events and (not c.execute("SELECT 1 FROM sqlite_master WHERE name='wk_person_events'").fetchone() or any(not c.execute('SELECT 1 FROM wk_person_events WHERE id=?',(i,)).fetchone() for i in events)):
                        raise StoreError('人员关系维护流水不存在。')
                    active=c.execute("SELECT value FROM wk_meta WHERE key='work_analysis_version'").fetchone()
                    if active and json.loads(active[0]) >= 34:
                        from work_analysis import WorkAnalysis, CLOSED
                        service=WorkAnalysis(self.store)
                        targets=set()
                        for change_id in proof:
                            change=c.execute('SELECT kind,record_id FROM wk_changes WHERE id=?',(change_id,)).fetchone()
                            if change['kind']=='tasks':targets.add(change['record_id'])
                            elif change['kind']=='projects' and not __import__('incremental').enabled(c):
                                targets.update(t['id'] for t in self.store._records(c,'tasks').values()
                                               if t.get('project_id')==change['record_id'] and t.get('_work_analysis'))
                        for target in targets:
                            task=self.store._record(c,'tasks',target)
                            if not task.get('archived') and task.get('state') not in CLOSED and service.view(c,task)['needs_review']:
                                raise StoreError('工作已变化，请先重新分析当前情况及含时间的下一步，再登记整理完成：'+task['title'],409)
                if mode == 'needs_user' and not r.get('question'): raise StoreError('需要用户判断时须保留具体问题。')
                proof_data = {'change_ids': proof, 'person_event_ids':events, 'question': r.get('question', '')}
                if work_review_id:
                    proof_data['work_review_id'] = work_review_id
                from work_model import enabled
                if mode == 'needs_user' and enabled(c):
                    card_ids = r.get('judgment_ids', [])
                    if not isinstance(card_ids, list) or any(not isinstance(x, str) for x in card_ids):
                        raise StoreError('待判断问题标识应为列表。')
                    card_ids = list(dict.fromkeys(card_ids + ([r['judgment_id']] if r.get('judgment_id') else [])))
                    if not card_ids:
                        raise StoreError('待判断须先保存可见的问题卡。')
                    for card_id in card_ids:
                        card = c.execute('SELECT payload,state FROM wk_judgment_cards WHERE id=?', (card_id,)).fetchone()
                        if not card or vid not in json.loads(card['payload']).get('receipt_ids', []) or (not old_receipt and card['state'] not in ('pending', 'deferred')):
                            raise StoreError('待判断须先保存可见的问题卡，附背景、依据、建议和本原文版本，再登记回执。')
                    if r.get('judgment_id'):
                        proof_data['judgment_id'] = r['judgment_id']
                    if 'judgment_ids' in r:
                        proof_data['judgment_ids'] = r['judgment_ids']
                stored = json_text(proof_data)
                old = c.execute('SELECT disposition,reason,proof FROM wk_review_receipts WHERE version_id=?', (vid,)).fetchone()
                if old:
                    if tuple(old) != (mode, reason, stored): raise StoreError('该原文已有不同整理回执；不覆盖。',409)
                    continue
                # Actual work/project changes, linked evidence outcomes and
                # questions all require a current follow-through assessment.
                for change_id in proof:
                    change=c.execute('SELECT kind,record_id FROM wk_changes WHERE id=?',(change_id,)).fetchone()
                    if change and change['kind'] in ('projects','tasks'):
                        coverage_targets.add((change['kind'],change['record_id']))
                review=lifecycle.get(c,vid)
                if review:
                    coverage_targets.update(('tasks',x['work_id']) for x in review['outcomes']
                        if not __import__('incremental').enabled(c) or x.get('action') != 'unchanged')
                for qid in card_ids:
                    card=c.execute('SELECT payload FROM wk_judgment_cards WHERE id=?',(qid,)).fetchone()
                    if card:
                        for option in json.loads(card['payload'])['options']:
                            coverage_targets.update((op['kind'],op['id']) for op in option['operations'] if op['kind'] in ('projects','tasks'))
                c.execute('INSERT INTO wk_review_receipts VALUES(?,?,?,?,?,?)', (vid, mode, reason, stored, now(), pid))
                c.execute('UPDATE raw_changes SET reviewed_at=? WHERE version_id=?', (now(), vid)); added += 1
            from follow_through import FollowThrough
            FollowThrough(self.store).require_current(c,coverage_targets)
            from work_priority import WorkPriority
            WorkPriority(self.store).require_current(c, coverage_targets)
            from project_priority import ProjectPriority
            ProjectPriority(self.store).require_current(c, coverage_targets)
        return {'registered': added, **self.status()}

    def review_check(self):
        """End-of-cycle check also catches legacy/project/decision-only gaps."""
        result=self.status()
        from review_inventory import status
        inventory=status(self.store)['summary']
        counts={k:result[k] for k in ('pending_review','pending_work_evidence','pending_work_analysis','pending_follow_through','pending_work_priority','pending_project_priority','pending_reminder_plans','pending_cloud_reminder_sync')}
        counts['uncovered_responsibilities']=inventory['uncovered_fields']
        return dict(ready=not any(counts.values()),checks=counts,judgments=result['judgments'],
                    source_health=result['source_health'],
                    description='ready只表示已取得资料的业务整理与承接检查完成；来源缺口和待你判断另列，不能声称全部资料最新或业务问题已解决。')

    def resolve(self, vid, answer):
        if len(answer.strip()) < 8: raise StoreError('请记录用户明确回答及后续维护结果。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            from work_model import enabled
            if enabled(c):
                raise StoreError('请通过问题卡确认或修改，业务写回后会同步关闭对应整理回执。', 409)
            row=c.execute("SELECT v.source_id FROM wk_review_receipts r JOIN raw_versions v ON v.id=r.version_id WHERE r.version_id=? AND r.disposition='needs_user'",(vid,)).fetchone()
            if not row:raise StoreError('没有对应的待用户判断事项。')
            self.raw.source(row[0],True,c)
            old=c.execute('SELECT answer FROM wk_review_resolutions WHERE version_id=?',(vid,)).fetchone()
            if old and old[0]!=answer:raise StoreError('已有处理记录，不覆盖。',409)
            c.execute('INSERT OR IGNORE INTO wk_review_resolutions VALUES(?,?,?)',(vid,answer,now()))
        return self.status()

    def discover_feishu(self, read=lark_read):
        require_feishu(discovery=True)
        chats = {}; token = None; seen = set()
        for _ in range(100):
            args = ['im', '+chat-list', '--types=p2p,group', '--page-size', '100']
            if token: args += ['--page-token', token]
            data = read(args)
            if not isinstance(data.get('chats'), list) or type(data.get('has_more')) is not bool:
                raise StoreError('飞书会话目录分页不完整。')
            for chat in data['chats']:
                if not chat.get('chat_id','').startswith('oc_'): raise StoreError('飞书会话身份缺失。')
                chats[chat['chat_id']] = chat
            if not data['has_more']: break
            token = data.get('page_token')
            if not token or token in seen: raise StoreError('飞书目录游标异常。')
            seen.add(token)
        else: raise StoreError('飞书目录超过分页上限；未登记。')
        added = 0
        for identity, chat in chats.items():
            with self.store.connect() as c:
                if c.execute('SELECT 1 FROM raw_sources WHERE id=?', (source_key('feishu','chat',identity),)).fetchone(): continue
            self.raw.register_work_chat('feishu', identity, chat.get('name') or identity,
                {'note':'本机用户在首次接入时确认所有可见飞书内容均为工作资料', 'evidence':['当前已登录飞书会话目录', {'chat_mode':chat.get('chat_mode'), 'external':chat.get('external')} ]},
                history_since())
            added += 1
        return {'visible_chats': len(chats), 'added_sources': added}

    def collect(self):
        if TRIAL_END and dt.datetime.fromisoformat(now()) > dt.datetime.fromisoformat(TRIAL_END):
            raise StoreError('已到本机设置的采集截止时间。')
        # OS releases this lock on a crash. It covers the full multi-source collection.
        import msvcrt
        with (self.root / 'collect.lock').open('a+b') as lock:
            lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
            try: msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError: raise StoreError('已有定期采集运行，本轮不重复启动。',409) from None
            try: return self._collect()
            finally:
                lock.seek(0); msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)

    def _collect(self):
        self.raw.recover_interrupted()
        rid = 'cycle-' + uuid.uuid4().hex; result = {'sources':[], 'discovery':{}}
        with self.store.connect() as c:
            c.execute("UPDATE wk_periodic_runs SET state='interrupted',finished_at=? WHERE state='collecting'",(now(),))
            c.execute('INSERT INTO wk_periodic_runs VALUES(?,?,?,?,?)', (rid,now(),None,'collecting','{}'))
        try:
            if settings().get('feishu', {}).get('enabled') and settings().get('feishu', {}).get('all_visible_work'):
                try: result['discovery'] = self.discover_feishu()
                except (StoreError, OSError) as exc: result['discovery'] = {'error':str(exc)}
                try:
                    result['materials_discovery']=self.discover_materials()
                except (StoreError,OSError) as exc:
                    result['materials_discovery']={'complete':False,'errors':[{'reason':str(exc)}]}
                try:
                    result['tasks_discovery']=self.discover_tasks()
                except (StoreError,OSError) as exc:
                    result['tasks_discovery']={'complete':False,'errors':[{'reason':str(exc)}]}
            else:
                for key in ('discovery', 'materials_discovery', 'tasks_discovery'):
                    result[key] = {'complete': True, 'skipped': True, 'reason': '本安装未启用飞书全范围自动发现；仅处理已批准来源。'}
            sources = [s for s in self.raw.overview()['sources'] if s['state']=='approved' and not s['config'].get('manual_only')]
            wechat_blocked=''
            for s in sources:
                try:
                    if s['platform']=='wechat' and wechat_blocked:
                        r={'state':'skipped','error':'微信共用读取条件未通过，本轮不重复扫描；该来源覆盖时间保持。','failure_scope':'wechat_shared_read'}
                    else:
                        r = update_one(self.raw,s['id'])
                    if r.get('failure_scope')=='wechat_shared_read':wechat_blocked=r['error']
                    item = {k:r.get(k) for k in ('run_id','state','records','new','changed','unchanged','error','failure_scope')}
                    item.update(source_id=s['id'], name=s['name'])
                    if r.get('receipt',{}).get('local_table_found') is False: item['local_history_missing'] = True
                except (StoreError,OSError) as exc: item = {'source_id':s['id'],'name':s['name'],'state':'failed','error':str(exc)}
                result['sources'].append(item)
                with self.store.connect() as c:
                    c.execute('UPDATE wk_periodic_runs SET result=? WHERE id=?',(json_text(result),rid))
                print(json.dumps(item,ensure_ascii=False),flush=True)
            state = 'partial' if result['discovery'].get('error') or not result['materials_discovery'].get('complete') or not result['tasks_discovery'].get('complete') or any(s['state']!='succeeded' for s in result['sources']) else 'collected'
        except Exception:
            state = 'interrupted'
            raise
        finally:
            with self.store.connect() as c:
                c.execute('UPDATE wk_periodic_runs SET finished_at=?,state=?,result=? WHERE id=?', (now(),state,json_text(result),rid))
            (self.root / (rid+'.json')).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        return {'cycle_id':rid,'state':state,'source_count':len(result['sources']),**self.status()}

    def discover_tasks(self, register=True):
        from feishu_tasks import discover
        report=discover(self.raw,register=register)
        report.pop('entries',None)
        if register:
            with self.store.connect() as c:
                c.execute("INSERT OR REPLACE INTO wk_meta VALUES('feishu_tasks_discovery_v36',?)",(json_text(dict(report,observed_at=now())),))
        return report

    def discover_materials(self, since=history_since(), register=True):
        from feishu_materials import discover
        report=discover(self.raw,since=since,register=register)
        # The run ledger needs counts, errors and exact new sources, not a second
        # copy of every user's document metadata on every two-hour run.
        report.pop('entries',None)
        return report


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser();p.add_argument('--db');sub=p.add_subparsers(dest='command',required=True)
    for command in ('status','collect','review-check'):
        q=sub.add_parser(command);q.add_argument('--summary',action='store_true');q.add_argument('--output')
    q=sub.add_parser('discover-materials');q.add_argument('--since',default=history_since());q.add_argument('--register',action='store_true');q.add_argument('--output',required=True)
    q=sub.add_parser('discover-tasks');q.add_argument('--register',action='store_true');q.add_argument('--output',required=True)
    q=sub.add_parser('baseline');q.add_argument('--input',required=True)
    q=sub.add_parser('packet');q.add_argument('--limit',type=int,default=100);q.add_argument('--output',required=True)
    q=sub.add_parser('finish');q.add_argument('--input',required=True)
    q=sub.add_parser('resolve');q.add_argument('version_id');q.add_argument('--answer',required=True)
    args=p.parse_args();job=Periodic(Store(args.db));job.initialize()
    if args.command=='baseline':result=job.baseline(args.input)
    elif args.command=='finish':result=job.finish(json.loads(Path(args.input).read_text('utf-8-sig')))
    elif args.command=='resolve':result=job.resolve(args.version_id,args.answer)
    elif args.command=='packet':
        result=job.packet(args.limit)
        with Path(args.output).open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
        result={'packet_id':result['packet_id'],'items':len(result['items']),'output':args.output}
    elif args.command=='discover-materials':
        from feishu_materials import discover
        result=discover(job.raw,since=args.since,register=args.register)
        with Path(args.output).open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
        result={k:v for k,v in result.items() if k!='entries'}
    elif args.command=='discover-tasks':
        result=job.discover_tasks(register=args.register)
        with Path(args.output).open('x',encoding='utf-8') as f:json.dump(result,f,ensure_ascii=False,indent=2)
    else:result=getattr(job,args.command.replace('-','_'))()
    if args.command in ('status','collect','review-check'):
        if args.output:
            Path(args.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        if args.summary:
            if args.command=='review-check':
                result={k:result[k] for k in ('ready','checks','judgments','description')}
            elif args.command=='status':
                result={k:v for k,v in result.items() if k.startswith('pending_') or k in ('reviewed','judgments','trial_end')}
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if args.command=='review-check' and not result['ready']:sys.exit(2)


if __name__=='__main__':
    try:main()
    except (StoreError,ValueError,OSError) as exc:
        print(json.dumps({'error':str(exc)},ensure_ascii=False));sys.exit(1)
