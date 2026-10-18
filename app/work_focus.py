"""User-owned work focus preferences, shared by the workbench and Codex."""
import json
from store import StoreError, json_text, now


class WorkFocus:
    KEY = 'work_focus'

    def __init__(self, store):
        self.store = store

    def _get(self, conn):
        row = conn.execute('SELECT value FROM wk_meta WHERE key=?', (self.KEY,)).fetchone()
        return json.loads(row['value']) if row else {'followed_person_ids': [], 'revision': 0}

    def get(self):
        with self.store.connect() as conn:
            return self._get(conn)

    def save(self, values, actor='本机工作台', reason='用户调整工作关注人'):
        if not isinstance(values, dict) or set(values) != {'revision', 'followed_person_ids'}:
            raise StoreError('请提交当前版本和关注人员名单。')
        ids = values['followed_person_ids']
        if not isinstance(ids, list) or len(ids) > 500 or any(not isinstance(v, str) or not v for v in ids):
            raise StoreError('关注人员名单格式不正确。')
        if type(values['revision']) is not int:
            raise StoreError('请先读取当前关注设置。')
        ids = list(dict.fromkeys(ids))
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            before = self._get(conn)
            if before['revision'] != values['revision']:
                raise StoreError('关注人已在其他地方修改，请重新读取后再保存；当前选择尚未保存。', 409)
            people = self.store._records(conn, 'people')
            people = {r['id']: r for r in people.values()}
            row = conn.execute("SELECT value FROM wk_meta WHERE key='current_user_id'").fetchone()
            me = json.loads(row['value']) if row else None
            if me in ids:
                raise StoreError('本人工作已单独展示，无需把自己设为关注人。')
            if any(v not in people or people[v].get('archived') for v in ids):
                raise StoreError('名单含不存在或已归档的人员，请核对后再保存。')
            if ids == before['followed_person_ids']:
                return dict(before, unchanged=True)
            after = {'followed_person_ids': ids, 'revision': before['revision'] + 1, 'updated_at': now()}
            def view(value):
                return {'name': '工作关注设置', 'followed_people': [people.get(v, {}).get('name', '已移除人员') for v in value['followed_person_ids']]}
            conn.execute('INSERT INTO wk_meta(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                         (self.KEY, json_text(after)))
            conn.execute('''INSERT INTO wk_changes(kind,record_id,action,before_payload,after_payload,
                before_revision,after_revision,happened_at,actor,reason,before_view,after_view)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                ('work_preferences', self.KEY, '设置关注人', json_text(before), json_text(after), before['revision'],
                 after['revision'], after['updated_at'], str(actor)[:100], str(reason)[:1000], json_text(view(before)), json_text(view(after))))
            return dict(after, change_id=conn.execute('SELECT last_insert_rowid()').fetchone()[0])
