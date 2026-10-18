"""Publish reviewed reminder batches to Feishu's hosted workflow.

This process only synchronizes the queue. The hosted workflow owns the timer
and delivery, so shutting down this computer does not stop published reminders.
"""
import datetime as dt
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

from store import StoreError, json_text, now
from work_analysis import digest
from work_reminders import WorkReminders, moment, iso

KEY = 'work_reminders_cloud_v39'
STATE_KEY = 'work_reminders_cloud_sync_v39'
SCHEMA = 'work-reminders-v39'
FIELDS = ('批次', '提醒时间', '提醒内容', '发送状态', '已发送时间', '节点索引')


class FeishuQueue:
    def __init__(self, store, settings):
        self.settings = settings
        self.directory = store.path.parent / 'workbench-runtime' / 'cloud-reminders'
        self.directory.mkdir(parents=True, exist_ok=True)

    def call(self, args, manifest=False):
        from runtime_config import lark_command, require_feishu
        require_feishu()
        p = subprocess.run([*lark_command(), *args, '--as', 'user'],
                           capture_output=True, encoding='utf-8', errors='replace', timeout=45,
                           cwd=self.directory, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            data = json.loads(p.stdout if p.returncode == 0 else p.stderr)
        except ValueError:
            raise StoreError('飞书云端同步未取得可确认的返回，保留现有队列等待核对。') from None
        if p.returncode or not (data.get('ok') or manifest and data.get('manifest_version')):
            code = str(data.get('error', {}).get('code', 'unknown'))
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', code):
                code = 'unknown'
            raise StoreError('飞书云端同步未完成（'+code+'），先核对登录、权限或网络。')
        return data if manifest else data['data']

    def workflow(self):
        return self.call(['base', '+workflow-get', '--base-token', self.settings['base_token'],
                          '--workflow-id', self.settings['workflow_id']])

    def records(self):
        args = ['base', '+record-list', '--base-token', self.settings['base_token'],
                '--table-id', self.settings['table_id'], '--format', 'ndjson',
                '--output', 'queue.ndjson', '--overwrite']
        for name in FIELDS:
            args.extend(['--field-id', name])
        data = self.call(args, manifest=True)
        if data.get('has_more'):
            raise StoreError('云端提醒队列未完整读回，本轮不覆盖或新增。')
        rows = [json.loads(line) for line in (self.directory/'queue.ndjson').read_text(encoding='utf-8').splitlines() if line.strip()]
        if len(rows) != data['records_count']:
            raise StoreError('云端提醒队列读回数量不一致。')
        return rows

    def put(self, fields, record_id=''):
        args = ['base', '+record-upsert', '--base-token', self.settings['base_token'],
                '--table-id', self.settings['table_id'], '--json', json_text(fields)]
        if record_id:
            args.extend(['--record-id', record_id])
        result = self.call(args)
        ids = result.get('record', {}).get('record_id_list', [])
        return record_id or (ids[0] if len(ids) == 1 else '')

    def messages(self, start):
        data = self.call(['im', '+chat-messages-list', '--chat-id', self.settings['chat_id'],
                          '--start', start, '--order', 'desc', '--page-size', '50',
                          '--page-all', '--page-limit', '5', '--no-reactions'])
        if data.get('has_more'):
            raise StoreError('云端提醒消息未完整读回，先保留发送状态，待核对回执。')
        return data.get('messages', [])


class CloudReminders:
    def __init__(self, store, api_factory=FeishuQueue):
        self.store = store
        self.local = WorkReminders(store)
        self.api_factory = api_factory

    def initialize(self):
        self.local.initialize()
        with self.store.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS wk_reminder_cloud_batches(
                id TEXT PRIMARY KEY, record_id TEXT NOT NULL, notify_at TEXT NOT NULL,
                members TEXT NOT NULL, last_payload TEXT NOT NULL,
                remote_state TEXT NOT NULL, error TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')

    @staticmethod
    def settings(c):
        row = c.execute('SELECT value FROM wk_meta WHERE key=?', (KEY,)).fetchone()
        return json.loads(row[0]) if row else {}

    @staticmethod
    def _meta(c, key, value):
        c.execute('INSERT INTO wk_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, json_text(value)))

    @staticmethod
    def _workflow_valid(workflow, cfg):
        sends = [s for s in workflow.get('steps', []) if s.get('type') == 'LarkMessageAction']
        if len(sends) != 1:
            return False
        data = sends[0].get('data', {})
        recipients = data.get('receiver', [])
        return (workflow.get('status') == 'enabled' and data.get('send_to_everyone') is False
                and len(recipients) == 1 and recipients[0].get('value_type') == 'user'
                and recipients[0].get('value', {}).get('id') == cfg['recipient_open_id']
                and digest(workflow.get('steps')) == cfg['workflow_hash'])

    def connect(self, values):
        """Bind only a verified, self-only hosted workflow to this exact database."""
        required = {'base_token', 'table_id', 'workflow_id', 'url', 'test_message_id', 'chat_id', 'sender_id'}
        if set(values) != required or any(not isinstance(v, str) or not v for v in values.values()):
            raise StoreError('请提供已核对的云端队列、工作流及本人测试消息。')
        if not re.fullmatch(r'[A-Za-z0-9]+', values['base_token']) or not values['table_id'].startswith('tbl') or not values['workflow_id'].startswith('wkf'):
            raise StoreError('云端队列身份格式不正确。')
        with self.store.connect() as c:
            existing = self.settings(c)
            if existing and existing.get('base_token') != values['base_token']:
                raise StoreError('已有云端队列，需先核对旧队列停用情况。')
            local = self.local._settings(c)
            proof = c.execute("SELECT after_payload FROM wk_reminder_audit WHERE kind='notice' AND record_id=?", (values['test_message_id'],)).fetchone()
            if not proof or json.loads(proof[0])['recipient_open_id'] != local['recipient_open_id']:
                raise StoreError('先读回本人云端定时测试消息并保存回执。')
        cfg = dict(values, enabled=True, database_path=str(self.store.path),
                   app_id=local['app_id'], recipient_open_id=local['recipient_open_id'], updated_at=now())
        workflow = self.api_factory(self.store, cfg).workflow()
        cfg['workflow_hash'] = digest(workflow.get('steps'))
        if not self._workflow_valid(workflow, cfg):
            raise StoreError('云端工作流未启用或接收对象不是本人。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            self._meta(c, KEY, cfg)
            self.local._audit(c, 'cloud-settings', KEY, existing, cfg, 'Codex按用户要求启用关机后提醒')
        return cfg

    def _snapshot(self, at):
        with self.store.connect() as c:
            settings = self.local._settings(c)
            entries = self.local._entries(c, at)
        active = settings.get('enabled') and at <= moment(settings['ends_at'])
        eligible = {e['id']: e for e in entries if active and not e['blocked_reason']
                    and e['state'] not in ('sent', 'reserved', 'cloud_reported_sent')
                    and moment(e['notify_at']) <= moment(settings['ends_at'])}
        token = digest([settings, [(e['id'], e['signature'], e['blocked_reason'], e['notify_at'], e['state']) for e in entries]])
        return eligible, token

    @staticmethod
    def _index(row):
        try:
            value = json.loads(row.get('节点索引') or '{}')
        except ValueError:
            return {}
        return value if isinstance(value, dict) and value.get('schema') == SCHEMA else {}

    @staticmethod
    def _same(a, b):
        def normalized(values):
            result = {k: values.get(k) for k in ('批次', '提醒时间', '提醒内容', '发送状态', '节点索引')}
            if result['提醒时间']:
                result['提醒时间'] = iso(moment(result['提醒时间']))
            return result
        return normalized(a) == normalized(b)

    def _payload(self, batch, items):
        rows = [{k: e[k] for k in ('id', 'work_id', 'key', 'title', 'action', 'kind', 'when', 'notify_at', 'signature')} for e in items]
        when = moment(batch['notify_at'])
        return {'批次': when.strftime('%m月%d日 %H:%M 工作提醒'), '提醒时间': iso(when),
                '提醒内容': self.local.message(items, when) if items else '本批提醒已取消。',
                '发送状态': ['待发送' if items else '已取消'],
                '节点索引': json_text({'schema': SCHEMA, 'batch_id': batch['id'], 'items': rows})}

    def _record_report(self, batch, row):
        index = self._index(row)
        members = set(json.loads(batch['members']))
        items = index.get('items', [])
        if any(e.get('id') not in members for e in items):
            raise StoreError('云端批次的节点身份与本机登记不一致，需人工核对。')
        with self.store.connect() as c:
            for item in items:
                c.execute("INSERT INTO wk_reminder_occurrences VALUES(?,?,'cloud_reported_sent') ON CONFLICT(id) DO NOTHING",
                          (item['id'], batch['id']))

    def record_receipt(self, batch_id, message):
        """Save a real read-back message; a workflow status alone is not a receipt."""
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            cfg = self.settings(c)
            row = c.execute('SELECT * FROM wk_reminder_cloud_batches WHERE id=?', (batch_id,)).fetchone()
            if not row or row['remote_state'] != '已发送':
                raise StoreError('该云端批次尚无已发送状态。')
            payload = json.loads(row['last_payload'])
            if (message.get('chat_id') != cfg['chat_id'] or message.get('sender_id') != cfg['sender_id']
                    or message.get('text') != payload['提醒内容'] or not re.fullmatch(r'om_[A-Za-z0-9]+', message.get('message_id', ''))):
                raise StoreError('飞书实际消息与本批接收会话、发送方或内容不一致。')
            items = self._index(payload)['items']
            old = c.execute('SELECT message_id FROM wk_reminder_batches WHERE id=?', (batch_id,)).fetchone()
            if old:
                if old[0] != message['message_id']:
                    raise StoreError('该批次已有不同的消息回执。')
                return {'unchanged': True}
            c.execute('INSERT INTO wk_reminder_batches VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                      (batch_id, cfg['recipient_open_id'], payload['提醒内容'], json_text(items), 'sent', 1,
                       row['created_at'], now(), '', message['message_id'], ''))
            c.execute("UPDATE wk_reminder_occurrences SET state='sent' WHERE batch_id=?", (batch_id,))
            self.local._audit(c, 'cloud-receipt', batch_id, {}, message, 'Codex核对飞书实际消息')
            return {'id': batch_id, 'message_id': message['message_id']}

    def status(self):
        with self.store.connect() as c:
            cfg = self.settings(c)
            rows = [dict(r) for r in c.execute('SELECT * FROM wk_reminder_cloud_batches ORDER BY notify_at,id')]
            state = c.execute('SELECT value FROM wk_meta WHERE key=?', (STATE_KEY,)).fetchone()
        counts = {}
        for row in rows:
            counts[row['remote_state']] = counts.get(row['remote_state'], 0) + 1
        sync = json.loads(state[0]) if state else {}
        _, token = self._snapshot(moment())
        needs_sync = bool(cfg and (sync.get('local_token') != token or sync.get('result', {}).get('delivery_attention') or sync.get('result', {}).get('error')))
        return {'configured': bool(cfg), 'settings': cfg, 'counts': counts, 'needs_sync': needs_sync,
                'batches': [{k:r[k] for k in ('id','record_id','notify_at','remote_state','error')} for r in rows],
                'sync': sync}

    def _collect_receipts(self, api, cfg):
        with self.store.connect() as c:
            batches = [dict(r) for r in c.execute('''SELECT b.* FROM wk_reminder_cloud_batches b
                LEFT JOIN wk_reminder_batches r ON r.id=b.id
                WHERE b.remote_state='已发送' AND r.id IS NULL''')]
        if not batches:
            return 0
        messages = api.messages(min(b['created_at'] for b in batches))
        pending = 0
        for batch in batches:
            body = json.loads(batch['last_payload'])['提醒内容']
            matching = [m for m in messages if not m.get('deleted') and m.get('chat_id') == cfg['chat_id']
                        and m.get('sender',{}).get('id') == cfg['sender_id']
                        and ('\n'+body+'\n') in (m.get('content') or '')]
            if len(matching) != 1:
                pending += 1
                continue
            self.record_receipt(batch['id'], {'message_id': matching[0]['message_id'],
                'chat_id': cfg['chat_id'], 'sender_id': cfg['sender_id'], 'text': body})
        return pending

    def sync(self, at=None, min_interval=0):
        at = moment(at)
        with self.store.connect() as c:
            cfg = self.settings(c)
        if not cfg.get('enabled') or cfg.get('database_path') != str(self.store.path):
            return {'stopped': True, 'reason': '当前数据库未启用云端同步'}
        eligible, token = self._snapshot(at)
        owner = str(uuid.uuid4())
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            state_row = c.execute('SELECT value FROM wk_meta WHERE key=?', (STATE_KEY,)).fetchone()
            state = json.loads(state_row[0]) if state_row else {}
            if state.get('lease_until') and moment(state['lease_until']) > at:
                return {'busy': True, 'delivery_mode': 'feishu_cloud'}
            if (state.get('local_token') == token and state.get('checked_at') and
                    (at-moment(state['checked_at'])).total_seconds() < min_interval):
                return dict(state.get('result', {}), cached=True)
            self._meta(c, STATE_KEY, dict(state, lease_owner=owner, lease_until=iso(at+dt.timedelta(minutes=10))))
        result = {'delivery_mode': 'feishu_cloud', 'created': 0, 'updated': 0, 'verified': 0, 'delivery_attention': 0}
        written = {}
        try:
            api = self.api_factory(self.store, cfg)
            workflow = api.workflow()
            if not self._workflow_valid(workflow, cfg):
                raise StoreError('云端工作流已暂停或配置发生变化，保留用户修改并先核对。')
            remote = api.records()
            indexed = {}
            for row in remote:
                bid = self._index(row).get('batch_id')
                if bid:
                    if bid in indexed:
                        raise StoreError('云端发现同一提醒批次的重复记录，先核对，不继续新增。')
                    indexed[bid] = row
            with self.store.connect() as c:
                batches = [dict(r) for r in c.execute('SELECT * FROM wk_reminder_cloud_batches ORDER BY notify_at,id')]
            assigned = {item for b in batches for item in json.loads(b['members'])}
            new = sorted((e for e in eligible.values() if e['id'] not in assigned), key=lambda e:(e['notify_at'],e['kind']!='agreed',e['work_id'],e['key']))
            while new:
                first = new.pop(0)
                if moment(first['notify_at']) <= at+dt.timedelta(seconds=60):
                    result['delivery_attention'] += 1
                    continue
                group = [first]
                while new and len(group)<20 and new[0]['notify_at']==first['notify_at']:
                    if len(self.local.message(group+[new[0]],moment(first['notify_at'])).encode('utf-8'))>11000:
                        break
                    group.append(new.pop(0))
                batch = {'id':str(uuid.uuid4()),'record_id':'','notify_at':first['notify_at'],
                         'members':json_text([e['id'] for e in group]),'last_payload':'{}','remote_state':'待同步',
                         'error':'','created_at':iso(at),'updated_at':iso(at)}
                with self.store.connect() as c:
                    c.execute('INSERT INTO wk_reminder_cloud_batches VALUES(:id,:record_id,:notify_at,:members,:last_payload,:remote_state,:error,:created_at,:updated_at)', batch)
                batches.append(batch)
            for batch in batches:
                row = indexed.get(batch['id'])
                if row:
                    remote_state = (row.get('发送状态') or ['未知'])[0]
                    if batch['record_id'] and row['record_id'] != batch['record_id']:
                        raise StoreError('云端批次记录身份发生变化，需先核对。')
                    if remote_state in ('已发送','发送中'):
                        if remote_state=='已发送':
                            self._record_report(batch,row)
                        elif at-moment(batch['notify_at'])>dt.timedelta(minutes=5):
                            result['delivery_attention']+=1
                        with self.store.connect() as c:
                            c.execute('UPDATE wk_reminder_cloud_batches SET record_id=?,last_payload=?,remote_state=?,error=?,updated_at=? WHERE id=?',
                                      (row['record_id'],json_text({k:row.get(k) for k in FIELDS}),remote_state,'',iso(at),batch['id']))
                        continue
                    last = json.loads(batch['last_payload'])
                    if last and not self._same(last,row):
                        if batch['error'] == '等待云端写入读回确认。':
                            result['delivery_attention'] += 1
                            continue
                        with self.store.connect() as c:
                            c.execute('UPDATE wk_reminder_cloud_batches SET remote_state=?,error=?,updated_at=? WHERE id=?',
                                      (remote_state,'云端内容或状态已由用户修改，保留修改，等待核对。',iso(at),batch['id']))
                        result['delivery_attention']+=1
                        continue
                elif batch['record_id'] or batch['error']:
                    result['delivery_attention']+=1
                    continue
                # Read current work again immediately before changing a cloud row.
                latest,_ = self._snapshot(at)
                items = [latest[i] for i in json.loads(batch['members']) if i in latest]
                payload = self._payload(batch, items)
                if row and self._same(row,payload):
                    result['verified']+=1
                else:
                    if items and moment(batch['notify_at'])<=at+dt.timedelta(seconds=60):
                        result['delivery_attention']+=1
                        continue
                    try:
                        record_id = api.put(payload, row['record_id'] if row else '')
                        if not record_id:
                            raise StoreError('云端写入未返回记录身份。')
                    except (StoreError,OSError,ValueError,subprocess.SubprocessError):
                        with self.store.connect() as c:
                            c.execute('UPDATE wk_reminder_cloud_batches SET error=?,updated_at=? WHERE id=?',
                                      ('云端写入结果未确认；先读回原批次，不盲目重建。',iso(at),batch['id']))
                        raise
                    result['updated' if row else 'created']+=1
                    written[batch['id']] = payload
                    row = dict(payload,record_id=record_id)
                with self.store.connect() as c:
                    c.execute('UPDATE wk_reminder_cloud_batches SET record_id=?,last_payload=?,remote_state=?,error=?,updated_at=? WHERE id=?',
                              (row['record_id'],json_text(payload),payload['发送状态'][0],
                               '等待云端写入读回确认。' if batch['id'] in written else '',iso(at),batch['id']))
            # Verify all writes together. A successful request is not a full readback.
            if written:
                fresh = {self._index(r).get('batch_id'):r for r in api.records() if self._index(r)}
                with self.store.connect() as c:
                    for batch_id, payload in written.items():
                        if batch_id not in fresh or not self._same(payload,fresh[batch_id]):
                            raise StoreError('云端写入后的统一读回尚不一致，保留批次，稍后重核。')
                        c.execute("UPDATE wk_reminder_cloud_batches SET error='' WHERE id=?", (batch_id,))
                result['verified']+=len(written)
            result['receipts_pending'] = self._collect_receipts(api, cfg)
            result['delivery_attention'] += result['receipts_pending']
            result['checked_at']=iso(at)
            _, token = self._snapshot(at)
            with self.store.connect() as c:
                self._meta(c,STATE_KEY,{'checked_at':iso(at),'local_token':token,'result':result})
            return result
        except Exception:
            with self.store.connect() as c:
                self._meta(c,STATE_KEY,{'checked_at':iso(at),'result':dict(result,error='云端同步未完成；云端已存计划继续按原安排执行。')})
            raise
        finally:
            with self.store.connect() as c:
                row=c.execute('SELECT value FROM wk_meta WHERE key=?',(STATE_KEY,)).fetchone()
                value=json.loads(row[0]) if row else {}
                if value.get('lease_owner')==owner:
                    value.pop('lease_owner',None);value.pop('lease_until',None)
                    self._meta(c,STATE_KEY,value)
