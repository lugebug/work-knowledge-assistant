"""Reviewed work reminders. Business dates are never inferred by the dispatcher.

The shared store remains authoritative. Plans are reviewed against current work,
analysis and evidence; delivery has its own ledger and does not complete work.
"""
import datetime as dt
import json
import re
import subprocess
import uuid

from store import StoreError, json_text, now
from work_analysis import WorkAnalysis, CLOSED, digest

TZ = dt.timezone(dt.timedelta(hours=8))
SETTINGS_KEY = 'work_reminders_v38'


def moment(value=None):
    value = dt.datetime.fromisoformat(value or now())
    if value.tzinfo is None:
        raise StoreError('提醒时刻必须包含时区。')
    return value.astimezone(TZ)


def iso(value):
    return value.astimezone(TZ).isoformat(timespec='seconds')


def feishu_send(settings, body, batch_id):
    """Use the existing credential store; never export tokens or app secrets."""
    from runtime_config import lark_command, require_feishu
    require_feishu()
    flags = dict(capture_output=True, encoding='utf-8', errors='replace', timeout=45,
                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    status = subprocess.run([*lark_command(), 'auth', 'status'], **flags)
    auth = json.loads(status.stdout)
    if auth.get('appId') != settings['app_id']:
        raise StoreError('飞书应用身份已变化，先重新核对本人接收账号。')
    result = subprocess.run([*lark_command(), 'im', '+messages-send', '--as', 'bot',
                            '--user-id', settings['recipient_open_id'], '--text', body,
                            '--idempotency-key', batch_id], **flags)
    data = json.loads(result.stdout)
    if not data.get('ok') or not data.get('data', {}).get('message_id'):
        # Keep credential-bearing CLI output out of the business database.
        raise StoreError('飞书未确认发送成功，请核对应用权限或网络后重试。')
    return data['data']['message_id']


class WorkReminders:
    def __init__(self, store):
        self.store = store
        self.analysis = WorkAnalysis(store)

    def initialize(self):
        with self.store.connect() as c:
            c.executescript('''
              CREATE TABLE IF NOT EXISTS wk_reminder_plans(
                work_id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
                context_token TEXT NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS wk_reminder_controls(
                work_id TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0,
                snoozed_until TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS wk_reminder_batches(
                id TEXT PRIMARY KEY, recipient TEXT NOT NULL, body TEXT NOT NULL,
                items TEXT NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, retry_at TEXT NOT NULL,
                message_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '');
              CREATE TABLE IF NOT EXISTS wk_reminder_occurrences(
                id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, state TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS wk_reminder_audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, record_id TEXT NOT NULL,
                before_payload TEXT NOT NULL, after_payload TEXT NOT NULL,
                at TEXT NOT NULL, actor TEXT NOT NULL);
            ''')
            columns = {r[1] for r in c.execute('PRAGMA table_info(wk_reminder_controls)')}
            for column in ('target_key', 'target_when'):
                if column not in columns:
                    c.execute('ALTER TABLE wk_reminder_controls ADD COLUMN '+column+" TEXT NOT NULL DEFAULT ''")

    def _audit(self, c, kind, rid, before, after, actor):
        c.execute('INSERT INTO wk_reminder_audit(kind,record_id,before_payload,after_payload,at,actor) VALUES(?,?,?,?,?,?)',
                  (kind, rid, json_text(before), json_text(after), now(), actor))

    def _settings(self, c):
        row = c.execute('SELECT value FROM wk_meta WHERE key=?', (SETTINGS_KEY,)).fetchone()
        return json.loads(row[0]) if row else {'revision': 0, 'enabled': False}

    def register_notice(self, message_id, text, sent_at):
        """Record an already read-back setup notice, without creating work."""
        if not re.fullmatch(r'om_[a-zA-Z0-9]+', message_id):
            raise StoreError('请提供飞书读回确认的消息身份。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT after_payload FROM wk_reminder_audit WHERE kind='notice' AND record_id=?", (message_id,)).fetchone()
            payload = dict(text=text, sent_at=iso(moment(sent_at)), recipient_open_id=self._settings(c)['recipient_open_id'])
            if row:
                if json.loads(row[0]) != payload:
                    raise StoreError('该通知已有不同发送记录。')
                return {'message_id': message_id, 'unchanged': True}
            self._audit(c, 'notice', message_id, {}, payload, 'Codex核对飞书实际消息')
            return {'message_id': message_id}

    @staticmethod
    def outgoing_notice(c, object_id):
        if not isinstance(object_id, str) or not object_id.startswith('feishu:om_'):
            return None
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='wk_reminder_batches'").fetchone():
            return None
        mid = object_id.removeprefix('feishu:')
        row = c.execute('SELECT id FROM wk_reminder_batches WHERE message_id=?', (mid,)).fetchone()
        notice = c.execute("SELECT 1 FROM wk_reminder_audit WHERE kind='notice' AND record_id=?", (mid,)).fetchone()
        if row or notice:
            return dict(message_id=mid, batch_id=row['id'] if row else '',
                        instruction='本系统已确认发给本人的提醒或通道说明，仅是已存工作的一次通知，不构成新的承诺、执行结果或独立工作。')
        return None

    def configure(self, values, actor='Codex按用户要求设置提醒'):
        required = {'revision', 'enabled', 'recipient_open_id', 'app_id', 'date_time',
                    'lead_minutes', 'ends_at'}
        if not isinstance(values, dict) or set(values) != required:
            raise StoreError('请提供当前设置版本、启用状态、本人接收身份及提醒时间规则。')
        if type(values['enabled']) is not bool or type(values['revision']) is not int:
            raise StoreError('提醒设置格式不正确。')
        if not re.fullmatch(r'\d{2}:\d{2}', values['date_time']):
            raise StoreError('按日期提醒的时间格式应为HH:MM。')
        try:
            dt.time.fromisoformat(values['date_time'])
            moment(values['ends_at'])
        except ValueError:
            raise StoreError('提醒时间格式不正确。') from None
        if type(values['lead_minutes']) is not int or not 0 <= values['lead_minutes'] <= 1440:
            raise StoreError('提前提醒分钟数须为0至1440。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = self._settings(c)
            if old['revision'] != values['revision']:
                raise StoreError('提醒设置已变化，请重新读取。', 409)
            me = json.loads(c.execute("SELECT value FROM wk_meta WHERE key='current_user_id'").fetchone()[0])
            person = self.store._record(c, 'people', me)
            ids = {a.get('account_id') for a in person.get('accounts', []) if a.get('platform') == 'feishu' and not a.get('archived')}
            if values['recipient_open_id'] not in ids:
                raise StoreError('本版仅向知识库中本人已关联的飞书账号发送提醒。')
            if not re.fullmatch(r'cli_[a-zA-Z0-9]+', values['app_id']):
                raise StoreError('请核对飞书应用身份。')
            clean = dict(values, revision=old['revision'] + 1, recipient_name=person['name'],
                         scope='mine', channel='feishu_bot', database_path=str(self.store.path), updated_at=now())
            c.execute('INSERT INTO wk_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                      (SETTINGS_KEY, json_text(clean)))
            self._audit(c, 'settings', SETTINGS_KEY, old, clean, actor)
            return clean

    def _context(self, c, record, project):
        from incremental import enabled, analysis_content
        if enabled(c):
            return digest({'work': self.analysis.context_token(c, record, project),
                           'analysis': analysis_content(record.get('_work_analysis') or {})})
        return digest({'work': self.analysis.context_token(c, record, project),
                       'analysis': record.get('_work_analysis') or {}})

    def _catalog(self, c):
        me = json.loads(c.execute("SELECT value FROM wk_meta WHERE key='current_user_id'").fetchone()[0])
        records = self.store._records(c, 'tasks')
        projects = self.store._records(c, 'projects')
        return me, records, projects

    @staticmethod
    def _mine(record, me):
        return record.get('owner_id') == me or me in (record.get('participant_ids') or [])

    def _reason(self, c, record, project, me):
        if record.get('archived') or record.get('state') in CLOSED:
            return '工作已结束'
        if not self._mine(record, me):
            return '不是本人明确负责或参与的工作'
        a = record.get('_work_analysis') or {}
        if not a or a.get('context_token') != self.analysis.context_token(c, record, project):
            return '工作或依据已变化，先更新分析和提醒'
        from incremental import source_delta, pending_source_edit
        if source_delta(c):
            from work_analysis import source_refs
            from work_completion import WorkCompletion
            goal = WorkCompletion.goal(c, record['id']) or {}
            refs = source_refs(record) + [ref for item in goal.get('criteria', []) for ref in item['sources']]
            if pending_source_edit(c, refs):
                return '引用依据出现未整理修订，先核对再提醒'
        if a.get('decision', {}).get('state') == 'dismissed':
            return '用户已否定当前建议，先重新整理'
        from work_completion import WorkCompletion
        completion = WorkCompletion(self.store)
        if completion.enabled(c) and completion.goal(c, record['id']):
            try:
                completion.validate(c, record, a.get('completion_check'), analysis=a)
            except StoreError:
                return '完成判断或时间归属需重新核对'
        return ''

    def packet(self, ids=None, limit=100):
        with self.store.connect() as c:
            c.execute('BEGIN')
            me, records, projects = self._catalog(c)
            plans = {r['work_id']: dict(r) for r in c.execute('SELECT * FROM wk_reminder_plans')}
            items = []
            for rid, record in records.items():
                if not self._mine(record, me) or record.get('archived') or record.get('state') in CLOSED:
                    continue
                token = self._context(c, record, projects.get(record.get('project_id'), {}))
                plan = plans.get(rid, {})
                if ids and rid not in ids or not ids and plan.get('context_token') == token:
                    continue
                items.append({'id': rid, 'work_revision': record['revision'], 'revision': plan.get('revision', 0),
                              'context_token': token, 'work': record,
                              'plan': json.loads(plan.get('payload', '{}')),
                              'blocked_reason': self._reason(c, record, projects.get(record.get('project_id'), {}), me)})
            return {'pending': len(items), 'items': items[:limit],
                    'instructions': '先处理最新事实与用户答复，按原目标判断完成并更新分析。逐项区分已约定和建议跟进；只把已有明确绝对日期或时刻的行动拆成节点，沿用稳定key。review_on只是分析复核日，不能直接当成承诺或行动时间。相对条件未发生、时间窗口未选定时写入notes，不擅自造日期；可以保存明确标注的合理跟进建议。已发送节点不改key重发；暂停与稍后提醒由用户控制。'}

    def save_plan(self, rid, values, actor='Codex核对工作提醒'):
        if not isinstance(values, dict) or set(values) != {'revision', 'work_revision', 'context_token', 'nodes', 'notes'}:
            raise StoreError('提醒计划需包含当前工作和提醒版本、节点及尚未确定的安排。')
        if not isinstance(values['nodes'], list) or len(values['nodes']) > 12:
            raise StoreError('每项工作最多保存12个明确提醒节点。')
        if not isinstance(values['notes'], str) or len(values['notes']) > 4000:
            raise StoreError('未确定安排的说明格式不正确。')
        nodes, keys = [], set()
        for node in values['nodes']:
            if not isinstance(node, dict) or set(node) != {'key', 'action', 'kind', 'when', 'basis'}:
                raise StoreError('每个节点须有稳定标识、行动、约定或建议、时间及依据。')
            if any(not isinstance(v, str) or not v.strip() for v in node.values()):
                raise StoreError('提醒节点内容不能为空。')
            if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,79}', node['key']) or node['key'] in keys:
                raise StoreError('提醒节点标识无效或重复。')
            if node['kind'] not in ('agreed', 'suggested') or len(node['action']) > 280 or len(node['basis']) > 2000:
                raise StoreError('请区分已有约定与建议，行动保持简洁。')
            try:
                if len(node['when']) == 10:
                    dt.date.fromisoformat(node['when'])
                else:
                    node = dict(node, when=iso(moment(node['when'])))
            except ValueError:
                raise StoreError('请使用明确的日期或带时区的时刻。') from None
            keys.add(node['key']); nodes.append(node)
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            me, records, projects = self._catalog(c)
            r = records.get(rid)
            if not r:
                raise StoreError('工作不存在。', 404)
            project = projects.get(r.get('project_id'), {})
            reason = self._reason(c, r, project, me)
            if reason:
                raise StoreError(reason, 409)
            token = self._context(c, r, project)
            row = c.execute('SELECT * FROM wk_reminder_plans WHERE work_id=?', (rid,)).fetchone()
            old = dict(row) if row else {'revision': 0}
            payload = {'nodes': nodes, 'notes': values['notes']}
            if token == old.get('context_token') and json_text(payload) == old.get('payload'):
                return {'id': rid, 'revision': old['revision'], 'unchanged': True}
            if values['revision'] != old['revision'] or values['work_revision'] != r['revision'] or values['context_token'] != token:
                raise StoreError('工作或提醒计划已变化，请重新读取后保存。', 409)
            revision = old['revision'] + 1
            c.execute('INSERT OR REPLACE INTO wk_reminder_plans VALUES(?,?,?,?,?)', (rid, revision, token, json_text(payload), now()))
            self._audit(c, 'plan', rid, old, dict(payload, revision=revision, context_token=token), actor)
            return {'id': rid, 'revision': revision, 'nodes': len(nodes)}

    def control(self, rid, action, until='', actor='Codex按用户要求调整提醒'):
        if action not in ('pause', 'resume', 'snooze'):
            raise StoreError('请选择暂停、恢复或稍后提醒。')
        if action == 'snooze' and moment(until) <= moment():
            raise StoreError('稍后提醒须指定未来时刻。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self.store._record(c, 'tasks', rid)
            row = c.execute('SELECT * FROM wk_reminder_controls WHERE work_id=?', (rid,)).fetchone()
            old = dict(row) if row else {}
            clean = dict(work_id=rid, paused=int(action == 'pause'),
                         snoozed_until=old.get('snoozed_until', ''), target_key=old.get('target_key', ''),
                         target_when=old.get('target_when', ''), updated_at=now())
            if action == 'snooze':
                settings = self._settings(c)
                plan = c.execute('SELECT payload FROM wk_reminder_plans WHERE work_id=?', (rid,)).fetchone()
                nodes = json.loads(plan[0])['nodes'] if plan else []
                if not nodes:
                    raise StoreError('当前没有明确提醒节点，请先按你的新安排补入一个节点。')
                def due(node):
                    target = (moment(node['when']+'T'+settings.get('date_time','09:00')+':00+08:00')
                              if len(node['when']) == 10 else moment(node['when'])-dt.timedelta(minutes=settings.get('lead_minutes',30)))
                    return target
                past = [node for node in nodes if due(node) <= moment()]
                chosen = max(past, key=due) if past else min(nodes, key=due)
                clean.update(snoozed_until=iso(moment(until)), target_key=chosen['key'], target_when=chosen['when'])
            c.execute('''INSERT OR REPLACE INTO wk_reminder_controls(work_id,paused,snoozed_until,updated_at,target_key,target_when)
                         VALUES(:work_id,:paused,:snoozed_until,:updated_at,:target_key,:target_when)''', clean)
            self._audit(c, 'control', rid, old, clean, actor)
            return clean

    def _entries(self, c, at):
        settings = self._settings(c)
        me, records, projects = self._catalog(c)
        controls = {r['work_id']: dict(r) for r in c.execute('SELECT * FROM wk_reminder_controls')}
        sent = {r['id']: r['state'] for r in c.execute('SELECT * FROM wk_reminder_occurrences')}
        result = []
        for row in c.execute('SELECT * FROM wk_reminder_plans'):
            rid = row['work_id']; r = records.get(rid)
            if not r:
                continue
            project = projects.get(r.get('project_id'), {})
            reason = self._reason(c, r, project, me)
            if row['context_token'] != self._context(c, r, project):
                reason = reason or '提醒依据已变化，等待重新整理'
            control = controls.get(rid, {})
            if control.get('paused'):
                reason = '用户已暂停提醒'
            for node in json.loads(row['payload'])['nodes']:
                target = (moment(node['when'] + 'T' + settings.get('date_time', '09:00') + ':00+08:00')
                          if len(node['when']) == 10 else moment(node['when']))
                notify = target if len(node['when']) == 10 else target - dt.timedelta(minutes=settings.get('lead_minutes', 30))
                override = (control.get('snoozed_until') if control.get('target_key') == node['key']
                            and control.get('target_when') == node['when'] else '')
                if override:
                    notify = moment(override)
                # An explicit "remind me again" creates one new notification,
                # without changing the business date or erasing the first receipt.
                occurrence = digest([rid, node['key'], node['when'], override] if override else [rid, node['key'], node['when']])
                state = sent.get(occurrence)
                if state == 'cancelled':
                    state = None
                state = state or ('blocked' if reason else 'missed' if at - notify > dt.timedelta(hours=24)
                                  else 'due' if notify <= at else 'scheduled')
                result.append(dict(node, id=occurrence, work_id=rid, title=r['title'], notify_at=iso(notify),
                                   state=state, blocked_reason=reason, signature=digest([node, row['context_token']]),
                                   context_token=row['context_token']))
        return sorted(result, key=lambda x: (x['notify_at'], x['kind'] != 'agreed', x['work_id'], x['key']))

    def status(self, at=None):
        at = moment(at)
        with self.store.connect() as c:
            c.execute('BEGIN')
            settings = self._settings(c)
            entries = self._entries(c, at)
            counts = {}
            for item in entries:
                counts[item['state']] = counts.get(item['state'], 0) + 1
            batches = [dict(r) for r in c.execute('SELECT id,state,attempts,created_at,updated_at,message_id,error FROM wk_reminder_batches ORDER BY created_at DESC LIMIT 20')]
            attention = c.execute("SELECT count(*) FROM wk_reminder_batches WHERE state IN ('retry','needs_check')").fetchone()[0]
            return {'settings': settings, 'active': bool(settings.get('enabled') and settings.get('database_path') == str(self.store.path)
                                                        and at <= moment(settings['ends_at'])),
                    'counts': counts, 'entries': entries, 'batches': batches, 'delivery_attention': attention, 'observed_at': iso(at)}

    @staticmethod
    def message(items, at):
        lines = [f'工作提醒｜{at:%m月%d日 %H:%M}', '']
        for item in items:
            label = '已约定' if item['kind'] == 'agreed' else '建议跟进'
            date = item['when'][5:10].replace('-', '月') + '日'
            if len(item['when']) > 10:
                date += ' ' + item['when'][11:16]
            lines.extend([f'【{label} · {date}】{item["title"]}', item['action'], ''])
        lines.append('建议跟进不是对方承诺。完成、改期或需要暂停时，在Codex告诉我或更新原工作。')
        return '\n'.join(lines)

    def dispatch(self, at=None, sender=None, dry_run=False):
        at = moment(at); sender = sender or feishu_send
        result = {'sent_batches': [], 'failed_batches': [], 'cancelled_batches': [], 'due': 0}
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            settings = self._settings(c)
            cloud = c.execute("SELECT value FROM wk_meta WHERE key='work_reminders_cloud_v39'").fetchone()
            if cloud and json.loads(cloud[0]).get('database_path') == str(self.store.path):
                return dict(result, delivery_mode='feishu_cloud', cloud_managed=True,
                            reason='已交飞书云端定时执行，本机不再直接发送。')
            if not settings.get('enabled') or settings.get('database_path') != str(self.store.path) or at > moment(settings['ends_at']):
                return dict(result, stopped=True, reason='提醒未启用、当前为数据库副本或试跑已结束')
            entries = self._entries(c, at)
            due = [x for x in entries if x['state'] == 'due' and not x['blocked_reason']]
            result['due'] = len(due)
            if dry_run:
                return dict(result, preview=self.message(due, at) if due else '', items=due)
            current = {x['id']: x for x in entries}
            batches = []
            for row in c.execute("SELECT * FROM wk_reminder_batches WHERE state IN ('sending','retry') ORDER BY created_at").fetchall():
                batch = dict(row)
                if moment(batch['retry_at']) > at:
                    continue
                old_items = json.loads(batch['items'])
                valid = all(x['id'] in current and not current[x['id']]['blocked_reason']
                            and current[x['id']]['signature'] == x['signature']
                            and moment(current[x['id']]['notify_at']) <= at for x in old_items)
                if batch['recipient'] != settings['recipient_open_id'] or not valid:
                    # A previous attempt may have reached Feishu despite a lost
                    # reply. Keep its reservation until its real outcome is read.
                    c.execute("UPDATE wk_reminder_batches SET state='needs_check',updated_at=?,error=? WHERE id=?",
                              (iso(at), '依据或设置已变化，旧发送结果需核对，已停止自动重试。', batch['id']))
                    continue
                if batch['attempts'] >= 3 or at - moment(batch['created_at']) > dt.timedelta(minutes=50):
                    c.execute("UPDATE wk_reminder_batches SET state='needs_check',updated_at=?,error=? WHERE id=?",
                              (iso(at), '发送结果仍不确定，停止自动重复；先核对飞书实际消息。', batch['id']))
                    continue
                if len(batches) < 2:
                    batches.append(batch)
            # One batch claims all selected occurrences atomically. Another run
            # sees the reservation and cannot send the same node concurrently.
            due = [x for x in self._entries(c, at) if x['state'] == 'due' and not x['blocked_reason']]
            while due and len(batches) < 2:
                group = []
                while due and len(group) < 20:
                    candidate = group + [due[0]]
                    if group and len(self.message(candidate, at).encode('utf-8')) > 11000:
                        break
                    group.append(due.pop(0))
                bid = str(uuid.uuid4())
                batch = dict(id=bid, recipient=settings['recipient_open_id'], body=self.message(group, at),
                             items=json_text(group), state='sending', attempts=0, created_at=iso(at),
                             updated_at=iso(at), retry_at=iso(at), message_id='', error='')
                c.execute('INSERT INTO wk_reminder_batches VALUES(:id,:recipient,:body,:items,:state,:attempts,:created_at,:updated_at,:retry_at,:message_id,:error)', batch)
                for item in group:
                    c.execute("INSERT OR REPLACE INTO wk_reminder_occurrences VALUES(?,?,'reserved')", (item['id'], bid))
                batches.append(batch)
            for batch in batches:
                c.execute("UPDATE wk_reminder_batches SET state='sending',attempts=attempts+1,updated_at=?,retry_at=? WHERE id=?",
                          (iso(at), iso(at + dt.timedelta(minutes=5)), batch['id']))
        for batch in batches:
            # Recheck business state immediately before each external write.
            with self.store.connect() as c:
                current_settings = self._settings(c)
                fresh = {x['id']: x for x in self._entries(c, at)}
                valid = (current_settings.get('enabled') and current_settings.get('recipient_open_id') == settings['recipient_open_id']
                         and current_settings.get('database_path') == str(self.store.path)
                         and current_settings.get('app_id') == settings['app_id'] and at <= moment(current_settings['ends_at']))
                valid = valid and all(x['id'] in fresh and not fresh[x['id']]['blocked_reason'] and fresh[x['id']]['signature'] == x['signature']
                            and moment(fresh[x['id']]['notify_at']) <= at
                            for x in json.loads(batch['items']))
                if not valid:
                    if batch['attempts']:
                        c.execute("UPDATE wk_reminder_batches SET state='needs_check',updated_at=?,error=? WHERE id=?",
                                  (iso(at), '重试前工作或设置变化，保留原发送占用，先核对飞书实际消息。', batch['id']))
                        continue
                    c.execute("UPDATE wk_reminder_batches SET state='cancelled',updated_at=? WHERE id=?", (iso(at), batch['id']))
                    c.execute("UPDATE wk_reminder_occurrences SET state='cancelled' WHERE batch_id=?", (batch['id'],))
                    result['cancelled_batches'].append(batch['id']); continue
            try:
                mid = sender(settings, batch['body'], batch['id'])
                if not mid:
                    raise StoreError('飞书未返回消息凭据。')
            except (StoreError, OSError, ValueError, subprocess.SubprocessError):
                with self.store.connect() as c:
                    c.execute("UPDATE wk_reminder_batches SET state='retry',updated_at=?,retry_at=?,error=? WHERE id=?",
                              (iso(at), iso(at + dt.timedelta(minutes=10)), '未取得可靠发送回执；使用同一消息标识有限重试。', batch['id']))
                result['failed_batches'].append(batch['id'])
            else:
                with self.store.connect() as c:
                    c.execute("UPDATE wk_reminder_batches SET state='sent',message_id=?,updated_at=?,error='' WHERE id=?", (mid, iso(at), batch['id']))
                    c.execute("UPDATE wk_reminder_occurrences SET state='sent' WHERE batch_id=?", (batch['id'],))
                result['sent_batches'].append({'id': batch['id'], 'message_id': mid})
        with self.store.connect() as c:
            result['delivery_attention'] = c.execute("SELECT count(*) FROM wk_reminder_batches WHERE state IN ('retry','needs_check')").fetchone()[0]
        return result
