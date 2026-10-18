"""Evidence-backed questions, separate from ordinary optional record blanks."""
from copy import deepcopy
from contextlib import nullcontext
import datetime as dt
import hashlib
import json

from model import FIELDS
from store import StoreError, json_text, now

CARD_TEXT = ('question', 'background', 'impact', 'recommendation', 'rationale', 'unknowns', 'checked')
CATEGORIES = ('decision', 'ambiguity', 'essential')


class JudgmentCards:
    def __init__(self, store):
        self.store = store

    def initialize(self):
        with self.store.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS wk_judgment_cards(
              id TEXT PRIMARY KEY, payload TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
              revision INTEGER NOT NULL DEFAULT 1, answer TEXT NOT NULL DEFAULT '',
              deferred_until TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS wk_judgment_events(
              id INTEGER PRIMARY KEY, card_id TEXT NOT NULL, action TEXT NOT NULL,
              before_json TEXT NOT NULL, after_json TEXT NOT NULL, actor TEXT NOT NULL, happened_at TEXT NOT NULL);
            ''')

    @staticmethod
    def exists(c):
        return bool(c.execute("SELECT 1 FROM sqlite_master WHERE name='wk_judgment_cards'").fetchone())

    def _operations(self, c, operations, allow_empty=False):
        if not isinstance(operations, list) or not 1 <= len(operations) <= 20:
            raise StoreError('每个处理选项需列出确认后实际更新的记录。')
        result = []
        seen = set()
        for operation in operations:
            if not isinstance(operation, dict) or set(operation) != {'kind', 'id', 'values'}:
                raise StoreError('判断后的业务变更格式不正确。')
            kind, rid = operation['kind'], operation['id']
            if kind not in FIELDS or kind in ('contacts', 'updates', 'capabilities', 'roadmap', 'demands'):
                raise StoreError('请将判断关联项目、工作、人员或组织。')
            if (kind, rid) in seen:
                raise StoreError('同一选项中对同一档案的变更应合在一起。')
            seen.add((kind, rid))
            record = self.store._record(c, kind, rid)
            if record.get('archived'):
                raise StoreError('判断涉及的档案已归档，请先核对范围。', 409)
            if any(key in operation['values'] for key in ('accounts', 'work_log', 'context_history', 'archived')):
                raise StoreError('身份合并、过程历史与归档需使用相应维护操作，不能由通用问题卡改写。')
            clean = {} if allow_empty and operation['values'] == {} else self.store._validate(c, kind, operation['values'])
            result.append(dict(kind=kind, id=rid, values=clean))
        return result

    def propose(self, card, actor='Codex依据整理', *, connection=None):
        """Save a reviewed question, never fabricate questions from empty fields."""
        if not isinstance(card, dict):
            raise StoreError('判断问题须为对象。')
        required = {'id', 'category', 'options', 'recommended_option', 'sources', *CARD_TEXT}
        allowed = required | {'editable', 'receipt_ids', 'due_date', 'completion', 'task_link_options'}
        if required - set(card) or set(card) - allowed:
            raise StoreError('问题需包含背景、影响、依据、建议及明确处理选项。')
        rid = card['id']
        if not isinstance(rid, str) or not rid or len(rid) > 160 or card['category'] not in CATEGORIES:
            raise StoreError('问题标识或类别无效。')
        for key in CARD_TEXT:
            if not isinstance(card[key], str) or len(card[key]) > 6000:
                raise StoreError('问题说明格式不正确。')
        if any(not card[k].strip() for k in ('question', 'background', 'impact', 'recommendation', 'rationale', 'checked')):
            raise StoreError('请先说明具体问题、已查资料、影响和建议理由。')
        with (nullcontext(connection) if connection is not None else self.store.connect()) as c:
            if connection is None:
                c.execute('BEGIN IMMEDIATE')
            from people import People
            payload = deepcopy(card)
            payload['sources'] = People._refs(c, card['sources'])
            options = payload['options']
            mappings = payload.get('task_link_options') or {}
            if not isinstance(mappings, dict):
                raise StoreError('任务关联选项格式无效。')
            from task_work_links import TaskWorkLinks
            task_links = TaskWorkLinks(self.store)
            if not isinstance(options, list) or not 1 <= len(options) <= 4:
                raise StoreError('请给出1至4个可直接采用或修改的处理选项。')
            ids = set()
            for option in options:
                if not isinstance(option, dict) or set(option) != {'id', 'label', 'description', 'operations'}:
                    raise StoreError('选项需说明选择含义及写回内容。')
                if not all(isinstance(option.get(k), str) and option[k].strip() for k in ('id', 'label', 'description')) or option['id'] in ids:
                    raise StoreError('选项名称为空或重复。')
                ids.add(option['id'])
                if option['id'] in mappings:
                    mapping = mappings[option['id']]
                    if not isinstance(mapping,dict) or set(mapping)!={'version_id','links','reason'} or not isinstance(mapping['reason'],str) or len(mapping['reason'])<8:
                        raise StoreError('任务关联选项须包含证据版本、原工作及关联理由。')
                    task_links._source(c,mapping['version_id'])
                    mapping['links']=task_links.validate_links(c,mapping['links'])
                    if mapping['version_id'] not in payload.get('receipt_ids',[]):
                        raise StoreError('任务关联判断必须关联对应原文回执。')
                option['operations'] = [] if option['operations']==[] and option['id'] in mappings else self._operations(c, option['operations'])
            if set(mappings)-ids:
                raise StoreError('任务关联引用了不存在的选项。')
            if payload['recommended_option'] not in ids:
                raise StoreError('推荐选项不存在。')
            from review_requirements import validate_completion
            validate_completion(payload)
            editable = payload.get('editable') or []
            if not isinstance(editable, list) or len(editable) > 4:
                raise StoreError('只展示本次判断直接涉及的局部修改，最多4项。')
            for field in editable:
                if not isinstance(field, dict) or {'operation', 'field', 'label', 'editor'} - set(field) or set(field) - {'operation', 'field', 'label', 'editor', 'required'}:
                    raise StoreError('可修改答案格式不正确。')
                if 'required' in field and type(field['required']) is not bool:
                    raise StoreError('必需答案标记格式不正确。')
                if field['editor'] not in ('text', 'textarea', 'people', 'projects', 'priority'):
                    raise StoreError('不支持的局部答案类型。')
                if type(field['operation']) is not int or field['operation'] < 0:
                    raise StoreError('可修改答案需对应一项明确操作。')
                for option in options:
                    if field['operation'] >= len(option['operations']) or field['field'] not in option['operations'][field['operation']]['values']:
                        raise StoreError('每个选项都需明确该答案写入的位置。')
            receipt_ids = payload.get('receipt_ids') or []
            if not isinstance(receipt_ids, list) or any(not isinstance(v, str) for v in receipt_ids):
                raise StoreError('来源整理回执格式不正确。')
            for vid in receipt_ids:
                if not c.execute("SELECT 1 FROM raw_versions v JOIN raw_sources s ON s.id=v.source_id WHERE v.id=? AND s.state='approved'", (vid,)).fetchone():
                    raise StoreError('关联原文版本不存在或来源未获准。')
            if payload.get('due_date'):
                try:
                    dt.date.fromisoformat(payload['due_date'])
                except (ValueError, TypeError):
                    raise StoreError('判断时间须有明确日期，未知时留空。') from None
            old = c.execute('SELECT * FROM wk_judgment_cards WHERE id=?', (rid,)).fetchone()
            if old:
                if old['state'] in ('resolved', 'dismissed'):
                    return dict(id=rid, revision=old['revision'], protected=True)
                if json.loads(old['payload']) == payload:
                    return dict(id=rid, revision=old['revision'], unchanged=True)
                c.execute('UPDATE wk_judgment_cards SET payload=?,revision=revision+1,updated_at=? WHERE id=?', (json_text(payload), now(), rid))
                before = dict(old)
            else:
                c.execute('INSERT INTO wk_judgment_cards(id,payload,created_at,updated_at) VALUES(?,?,?,?)', (rid, json_text(payload), now(), now()))
                before = {}
            after = dict(c.execute('SELECT * FROM wk_judgment_cards WHERE id=?', (rid,)).fetchone())
            self._event(c, rid, '更新问题' if old else '提出问题', before, after, actor)
            return dict(id=rid, revision=after['revision'])

    @staticmethod
    def _event(c, rid, action, before, after, actor):
        c.execute('INSERT INTO wk_judgment_events(card_id,action,before_json,after_json,actor,happened_at) VALUES(?,?,?,?,?,?)',
                  (rid, action, json_text(before), json_text(after), actor, now()))

    def _card(self, c, row):
        card = json.loads(row['payload'])
        targets, revisions = [], []
        seen = set()
        for option in card['options']:
            for operation in option['operations']:
                key = (operation['kind'], operation['id'])
                if key in seen:
                    continue
                seen.add(key)
                record = self.store._records(c, key[0]).get(key[1])
                revisions.append([*key, record.get('revision') if record else None, bool(record and record.get('archived'))])
                targets.append(dict(kind=key[0], id=key[1], name=(record or {}).get('name') or (record or {}).get('title') or '关联记录已不可用',
                                    revision=(record or {}).get('revision'), state=(record or {}).get('state', ''),
                                    current=(record or {}).get('progress') or (record or {}).get('stage') or '',
                                    values={field: (record or {}).get(field, '') for opt in card['options'] for op in opt['operations'] if (op['kind'], op['id']) == key for field in op['values']}))
        link_revisions=[]
        from task_work_links import TaskWorkLinks
        task_links=TaskWorkLinks(self.store)
        for mapping in (card.get('task_link_options') or {}).values():
            source=c.execute('SELECT s.id,s.state,h.version_id FROM raw_versions v JOIN raw_sources s ON s.id=v.source_id LEFT JOIN raw_heads h ON h.source_id=s.id AND h.object_id=v.object_id WHERE v.id=?',(mapping['version_id'],)).fetchone()
            binding=task_links.get(c,source['id']) if source else None
            link_revisions.append([source['id'] if source else '',binding['revision'] if binding else 0,source['version_id'] if source else '',source['state'] if source else 'missing',mapping['version_id']])
            for link in mapping['links']:
                key=('tasks',link['work_id'])
                if key in seen: continue
                seen.add(key); record=self.store._records(c,'tasks').get(link['work_id'])
                revisions.append([*key,record.get('revision') if record else None,bool(record and record.get('archived'))])
                targets.append(dict(kind='tasks',id=link['work_id'],name=(record or {}).get('title','关联工作已不可用'),
                    revision=(record or {}).get('revision'),state=(record or {}).get('state',''),current=(record or {}).get('progress',''),values={}))
        card.update(state=row['state'], revision=row['revision'], answer=row['answer'], deferred_until=row['deferred_until'],
                    updated_at=row['updated_at'], targets=targets,
                    unavailable=any(rev[2] is None or rev[3] for rev in revisions) or any(x[3]!='approved' or x[2]!=x[4] for x in link_revisions))
        card['version'] = hashlib.sha256(json_text([dict(row), revisions, link_revisions]).encode()).hexdigest()
        card['reminder_due'] = bool(row['state'] == 'deferred' and row['deferred_until'] and row['deferred_until'] <= now()[:10])
        from review_requirements import project_view
        return project_view(self.store, c, card)

    def queue_in_connection(self, c, relevance_index=None):
        if not self.exists(c):
            return []
        cards = [self._card(c, dict(row)) for row in c.execute('SELECT * FROM wk_judgment_cards ORDER BY created_at,id')]
        from relevance import build, question_score
        if relevance_index is None:
            from people import People
            from model import KINDS
            records = {kind:list(self.store._records(c,kind).values()) for kind in KINDS}
            meta = {r['key']:json.loads(r['value']) for r in c.execute('SELECT * FROM wk_meta')}
            relevance_index = build(records,meta,People(self.store).index(c))
        for card in cards:
            card['relevance_rank'] = question_score(relevance_index,card)
        cards.sort(key=lambda r: (r['state'] != 'pending' and not r['reminder_due'], r['relevance_rank'], r.get('due_date') or '9999-12-31', r['id']))
        return cards

    def summary(self, c, cards=None):
        cards = self.queue_in_connection(c) if cards is None else cards
        pending = sum(r['state'] == 'pending' for r in cards)
        deferred = sum(r['state'] == 'deferred' for r in cards)
        return dict(pending=pending, deferred=deferred, unresolved=pending+deferred,
                    resolved=sum(r['state'] == 'resolved' for r in cards),
                    dismissed=sum(r['state'] == 'dismissed' for r in cards),
                    reminder_due=sum(r['reminder_due'] for r in cards))

    def queue(self):
        with self.store.connect() as c:
            c.execute('BEGIN')
            cards=self.queue_in_connection(c)
            return dict(items=cards, summary=self.summary(c,cards))

    def decide(self, rid, version, request):
        if not isinstance(request, dict) or set(request) - {'action', 'option', 'edits', 'note', 'deferred_until'}:
            raise StoreError('请确认建议、修改答案，或暂缓处理。')
        action = request.get('action')
        if action not in ('confirm', 'edit', 'defer', 'dismiss', 'reopen'):
            raise StoreError('不支持的判断操作。')
        note = request.get('note', '')
        if not isinstance(note, str) or len(note) > 6000:
            raise StoreError('说明格式不正确或过长。')
        with self.store.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            raw = c.execute('SELECT * FROM wk_judgment_cards WHERE id=?', (rid,)).fetchone()
            if not raw:
                raise StoreError('判断问题不存在。', 404)
            before = dict(raw)
            card = self._card(c, before)
            if version != card['version']:
                raise StoreError('问题或相关资料已更新，本次未保存。请重新读取核对，输入仍保留。', 409)
            if card['state'] in ('resolved', 'dismissed') and action != 'reopen':
                raise StoreError('这项已处理；需要改变决定时，请先重新打开。', 409)
            result, changes, deferred_until = [], [], ''
            if action in ('confirm', 'edit'):
                if card['unavailable']:
                    raise StoreError('关联记录已归档或不可用，请先核对问题范围。', 409)
                option = next((v for v in card['options'] if v['id'] == request.get('option', card['recommended_option'])), None)
                if not option:
                    raise StoreError('请选择一个处理方案。')
                operations = deepcopy(option['operations'])
                edits = request.get('edits') or {}
                if not isinstance(edits, dict) or action == 'confirm' and edits:
                    raise StoreError('修改建议时请使用修改后确认。')
                fields = {str(i): item for i, item in enumerate(card.get('editable') or [])}
                if set(edits) - set(fields):
                    raise StoreError('只能修改本问题展示的答案。')
                for index, value in edits.items():
                    spec = fields[index]
                    if spec.get('locked'):
                        raise StoreError('这项资料已从关联档案补齐，不能用旧建议覆盖；请重新读取当前问题。', 409)
                    operations[spec['operation']]['values'][spec['field']] = value
                for index, operation in enumerate(operations):
                    if (operation['kind'] == 'projects' and operation['values'].get('decision') == '同意建议'
                            and operation['values'] != option['operations'][index]['values']):
                        operation['values']['decision'] = '调整后推进'
                task_mapping=(card.get('task_link_options') or {}).get(option['id'])
                operations = [] if operations==[] and task_mapping else self._operations(c, operations, allow_empty=True)
                for spec in card.get('editable') or []:
                    if spec.get('locked'):
                        continue
                    value = operations[spec['operation']]['values'][spec['field']]
                    from review_requirements import meaningful
                    if spec.get('required') and not meaningful(value):
                        raise StoreError('请明确“' + spec['label'] + '”；暂时无法判断可选择稍后处理，不能用空白确认解决。')
                for operation in operations:
                    if not operation['values']:
                        continue
                    record = self.store._record(c, operation['kind'], operation['id'])
                    values = deepcopy(operation['values'])
                    if (rid.startswith('work-lifecycle-') and operation['kind'] == 'tasks'
                            and values.get('state') in ('已完成', '已取消') and not values.get('result', '').strip()):
                        raise StoreError('确认关闭工作时请保留实际完成或取消结果。')
                    if operation['kind'] == 'tasks':
                        from work_model import entry
                        values['work_log'] = [*(record.get('work_log') or []), entry(
                            '用户确认：' + card['question'] + '\n' + (values.get('next_action') or note or option['label']),
                            mode='user', sources=card['sources'], origin='judgment:' + rid,
                            actor='用户', key='judgment-' + rid + '-' + str(card['revision']))]
                    saved = self.store.update_in_transaction(c, operation['kind'], operation['id'], values, record['revision'],
                                                            '用户', '待你判断：' + card['question'] + ('；' + note if note else ''), action_override='判断确认')
                    current = self.store._record(c, operation['kind'], operation['id'])
                    if any(current.get(k, '') != v for k, v in operation['values'].items()):
                        raise StoreError('答案写回核对未通过，本次操作已回滚。', 409)
                    result.append(dict(kind=operation['kind'], **saved))
                    changes.append(dict(kind=operation['kind'], id=operation['id'], values=operation['values']))
                if task_mapping:
                    from task_work_links import TaskWorkLinks
                    service=TaskWorkLinks(self.store)
                    source=service._source(c,task_mapping['version_id'])
                    binding=service.get(c,source['id'])
                    saved=service.bind(dict(task_mapping,revision=binding['revision'] if binding else 0),actor='用户',connection=c)
                    result.append(dict(kind='task-link',id=source['id'],revision=saved['revision']))
                    names=[self.store._record(c,'tasks',x['work_id'])['title'] for x in task_mapping['links']]
                    changes.append(dict(kind='task-link',id=source['id'],values={'association':'归入：'+'、'.join(names) if names else '本次不归入现有工作，不自动新建。'}))
                state = 'resolved'
                answer = json_text(dict(option=option['label'], changes=changes, note=note))
            elif action == 'defer':
                state, answer = 'deferred', note or '暂不知道／稍后处理'
                deferred_until = request.get('deferred_until') or ''
                if deferred_until:
                    try:
                        dt.date.fromisoformat(deferred_until)
                    except (ValueError, TypeError):
                        raise StoreError('再次提醒日期格式不正确。') from None
            elif action == 'dismiss':
                if not note.strip():
                    raise StoreError('请用一句话说明前提哪里不对，或为什么不用处理。')
                state, answer = 'dismissed', note.strip()
            else:
                state, answer = 'pending', note
            if card.get('task_link_options') and action in ('dismiss','reopen'):
                from task_work_links import TaskWorkLinks
                service=TaskWorkLinks(self.store); mappings=list(card['task_link_options'].values())
                source=service._source(c,mappings[0]['version_id']); binding=service.get(c,source['id'])
                if action=='dismiss':
                    service.bind(dict(version_id=mappings[0]['version_id'],revision=binding['revision'],links=[],reason=note.strip()+'；用户撤回本次关联问题，不自动新建。'),actor='用户',connection=c)
                else:
                    service._write(c,source['id'],dict(status='pending',links=[],version_id=mappings[0]['version_id'],
                        reason='用户明确重新打开任务关联问题。',mode='user',judgment_id=rid,
                        candidates=[link for mapping in mappings for link in mapping['links']]),'用户')
            c.execute('UPDATE wk_judgment_cards SET state=?,answer=?,deferred_until=?,revision=revision+1,updated_at=? WHERE id=?',
                      (state, answer, deferred_until, now(), rid))
            if state in ('resolved', 'dismissed'):
                for vid in card.get('receipt_ids') or []:
                    siblings = [json.loads(row['payload']).get('receipt_ids', []) for row in c.execute("SELECT payload FROM wk_judgment_cards WHERE state IN ('pending','deferred')")]
                    if not any(vid in ids for ids in siblings) and c.execute('SELECT 1 FROM wk_review_receipts WHERE version_id=?', (vid,)).fetchone():
                        c.execute('INSERT OR IGNORE INTO wk_review_resolutions VALUES(?,?,?)',
                                  (vid, '用户处理判断「' + card['question'] + '」：' + answer, now()))
            elif action == 'reopen':
                for vid in card.get('receipt_ids') or []:
                    c.execute('DELETE FROM wk_review_resolutions WHERE version_id=?', (vid,))
            after = dict(c.execute('SELECT * FROM wk_judgment_cards WHERE id=?', (rid,)).fetchone())
            self._event(c, rid, action, before, after, '用户')
            if changes:
                from review_requirements import reconcile_in_transaction
                reconcile_in_transaction(self.store,c,{(v['kind'],v['id']) for v in changes})
            return dict(id=rid, state=state, saved=result, revision=after['revision'])

    def reconcile(self):
        from review_requirements import reconcile_in_transaction
        with self.store.connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            return {'resolved': reconcile_in_transaction(self.store, conn)}
