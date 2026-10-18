"""Suggested new work remains separate until the user adopts or links it."""
from copy import deepcopy
import hashlib
import json
import re

from store import StoreError, json_text, now
from work_model import entry


def normalized_title(value):
    return re.sub(r'[\W_]+', '', (value or '').casefold())


def exact_matches(store, c, values):
    title = normalized_title(values.get('title'))
    return [r for r in store._records(c, 'tasks', True).values()
            if title and normalized_title(r.get('title')) == title
            and (r.get('project_id') or '') == (values.get('project_id') or '')]


class WorkProposals:
    def __init__(self, store):
        self.store = store

    def initialize(self):
        with self.store.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS wk_work_proposals(
                id TEXT PRIMARY KEY,payload TEXT NOT NULL,revision INTEGER NOT NULL,
                state TEXT NOT NULL,updated_at TEXT NOT NULL)''')

    def rows(self, c):
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='wk_work_proposals'").fetchone():
            return []
        return [dict(json.loads(r['payload']), id=r['id'], revision=r['revision'],
                     state=r['state'], updated_at=r['updated_at'])
                for r in c.execute('SELECT * FROM wk_work_proposals ORDER BY updated_at DESC,id')]

    def candidates(self, values):
        if not isinstance(values, dict) or not isinstance(values.get('title'), str):
            raise StoreError('请提供工作标题及已知项目。')
        with self.store.connect() as c:
            exact = exact_matches(self.store, c, values)
            records = self.store._records(c, 'tasks', True).values()
            related = [r for r in records if values.get('project_id') and r.get('project_id') == values['project_id']]
            def small(r):
                return {k: r.get(k) for k in ('id', 'revision', 'title', 'state', 'archived', '_removed', 'project_id', 'progress', 'next_action')}
            return {'exact': [small(r) for r in exact], 'same_project': [small(r) for r in related],
                    'instruction': '结合目标和实际结果判断是否同一工作。标题相近只作线索；已完成、取消、归档或撤销不自动重开。'}

    def _write(self, c, rid, payload, before, revision, state, action, actor):
        stamp = now()
        c.execute('''INSERT INTO wk_work_proposals VALUES (?,?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,revision=excluded.revision,
          state=excluded.state,updated_at=excluded.updated_at''',
                  (rid, json_text(payload), revision + 1, state, stamp))
        def view(value):
            return {'title': value.get('title', ''), 'progress': value.get('current', ''),
                    'next_action': value.get('next_step', ''), 'state': value.get('decision', {}).get('state', 'pending')}
        c.execute('''INSERT INTO wk_changes(kind,record_id,action,before_payload,after_payload,
            before_revision,after_revision,happened_at,actor,reason,before_view,after_view)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            ('work_proposals', rid, action, json_text(before), json_text(payload), revision, revision + 1,
             stamp, actor, '建议新工作与明确安排分别保存', json_text(view(before)), json_text(view(payload))))
        return {'id': rid, 'revision': revision + 1, 'state': state,
                'change_id': c.execute('SELECT last_insert_rowid()').fetchone()[0]}

    def propose(self, values, actor='Codex工作整理'):
        allowed = {'key', 'title', 'project_id', 'organization_id', 'current', 'next_step', 'rationale', 'sources'}
        if not isinstance(values, dict) or set(values) != allowed:
            raise StoreError('新工作建议须包含稳定标识、标题、当前情况、含时间的下一步、原因和依据。')
        clean = deepcopy(values)
        for k in allowed - {'sources'}:
            if not isinstance(clean[k], str) or len(clean[k]) > (12000 if k in ('current', 'next_step', 'rationale') else 500):
                raise StoreError('工作建议内容格式不正确。')
            clean[k] = clean[k].strip()
        if any(not clean[k] for k in ('key', 'title', 'current', 'next_step', 'rationale')):
            raise StoreError('请说明建议做什么、为什么做以及下一步的时间。')
        rid = 'proposal-' + hashlib.sha256(clean['key'].encode()).hexdigest()[:24]
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old = next((r for r in self.rows(c) if r['id'] == rid), None)
            if old:
                # A repeated source review cannot reopen, relabel or replace a
                # proposal the user has already handled.
                return {'id': rid, 'revision': old['revision'], 'state': old['state'],
                        'unchanged': True, 'work_id': old.get('work_id', '')}
            duplicates = exact_matches(self.store, c, clean)
            if duplicates:
                raise StoreError('已有同项目同名工作，请读取并更新现有工作：' + '、'.join(r['id'] for r in duplicates), 409)
            for proposal in self.rows(c):
                if normalized_title(proposal['title']) == normalized_title(clean['title']) and proposal.get('project_id', '') == clean['project_id']:
                    return {'id': proposal['id'], 'revision': proposal['revision'], 'state': proposal['state'], 'unchanged': True}
            self.store._validate(c, 'tasks', {k: clean[k] for k in ('title', 'project_id', 'organization_id')}, True)
            from people import People
            clean['sources'] = People._refs(c, clean['sources'])
            clean['decision'] = {'state': 'pending'}
            return self._write(c, rid, clean, {}, 0, 'pending', '建议新工作', actor)

    def decide(self, rid, revision, answer, actor='本机工作台'):
        allowed = {'action', 'title', 'next_step', 'target_id', 'target_revision', 'note'}
        if not isinstance(answer, dict) or set(answer) - allowed or answer.get('action') not in ('accept', 'edit', 'dismiss', 'link'):
            raise StoreError('请选择采纳为我的工作、修改后采纳、归入已有工作或不采纳。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT * FROM wk_work_proposals WHERE id=?', (rid,)).fetchone()
            if not row:
                raise StoreError('工作建议不存在。', 404)
            if type(revision) is not int or row['revision'] != revision or row['state'] != 'pending':
                raise StoreError('建议已更新或已处理，请重新读取；不会重复创建工作。', 409)
            before = json.loads(row['payload']); payload = deepcopy(before)
            action = answer['action']
            note = answer.get('note', '')
            if not isinstance(note, str) or len(note) > 2000:
                raise StoreError('说明格式不正确。')
            work_result = None
            if action != 'dismiss':
                # Revalidate source permission at acceptance, not only proposal time.
                from people import People
                refs = People._refs(c, payload['sources'])
                title = answer.get('title', payload['title']) if action == 'edit' else payload['title']
                next_step = answer.get('next_step', payload['next_step'])
                self.store._validate(c, 'tasks', {'title': title, 'next_action': next_step}, True)
                if not next_step.strip():
                    raise StoreError('请保留含时间安排的下一步。')
                if action == 'link':
                    target = self.store._record(c, 'tasks', answer.get('target_id'))
                    if type(answer.get('target_revision')) is not int or target['revision'] != answer['target_revision']:
                        raise StoreError('所选工作已更新，请重新读取。', 409)
                    if target.get('archived') or target.get('state') in ('已完成', '已取消'):
                        raise StoreError('不能把新建议自动归入已结束的工作；请先核对如何继续。', 409)
                    log = entry('用户采纳并归入本工作：' + title + '。' + next_step,
                                date=now()[:10], mode='user', sources=refs,
                                actor=actor, key='adopt-' + rid)
                    work_result = self.store.update_in_transaction(c, 'tasks', target['id'],
                        {'work_log': [*(target.get('work_log') or []), log], 'next_action': next_step},
                        target['revision'], actor, '将建议归入已有工作')
                else:
                    duplicates = exact_matches(self.store, c, dict(payload, title=title))
                    if duplicates:
                        raise StoreError('已有相同工作，请使用“归入已有工作”，避免重复创建。', 409)
                    me = c.execute("SELECT value FROM wk_meta WHERE key='current_user_id'").fetchone()
                    if not me:
                        raise StoreError('请先在工作台明确本人身份。')
                    values = dict(title=title, project_id=payload['project_id'], organization_id=payload['organization_id'],
                                  owner_id=json.loads(me[0]), state='待办', progress=payload['current'],
                                  next_action=next_step, background=payload['rationale'])
                    clean = self.store._validate(c, 'tasks', values, True)
                    clean['_dossier_sources'] = refs
                    clean['_work_proposal_id'] = rid
                    clean['_provenance'] = {k: dict(mode='user', actor=actor, at=now(), sources=refs) for k in values}
                    work_id = 'work-adopt-' + rid.removeprefix('proposal-')
                    if work_id in self.store._records(c, 'tasks', True):
                        raise StoreError('此建议对应的工作已存在或已撤销，不能重新创建。', 409)
                    work_result = self.store._write(c, 'tasks', work_id, clean, {'_removed': True}, 0,
                                                   '采纳新工作', actor, '用户明确采纳为我的工作', True)
                payload.update(title=title, next_step=next_step, work_id=work_result['id'])
            state = 'dismissed' if action == 'dismiss' else 'linked' if action == 'link' else 'accepted'
            payload['decision'] = dict(state=state, at=now(), actor=actor, note=note)
            result = self._write(c, rid, payload, before, revision, state,
                                 {'dismiss': '不采纳新工作', 'link': '归入已有工作', 'accept': '采纳新工作', 'edit': '修改后采纳新工作'}[action], actor)
            return dict(result, work=work_result)
