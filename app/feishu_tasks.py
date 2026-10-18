"""Read-only Feishu task snapshots and discovery, independent of work decisions."""
from copy import deepcopy
import datetime as dt
import re
from urllib.parse import urlparse, parse_qs

from raw_collectors import lark_read, event_time, visible_text, TZ
from store import StoreError

from runtime_config import history_since, require_feishu
SINCE = history_since()
GUID = re.compile(r'^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$')


def guid(value):
    if not isinstance(value, str) or not GUID.fullmatch(value):
        raise StoreError('飞书任务或清单身份无效。')
    return value.lower()


def timestamp(value):
    if value in (None, '', '0', 0):
        return ''
    try:
        return event_time(value)
    except (ValueError, TypeError, OverflowError, OSError):
        raise StoreError('飞书任务时间格式不完整；未推进覆盖位置。') from None


def in_scope(task, since=SINCE):
    # A still-open old task is a current snapshot, not a backfill of old history.
    if task.get('status') == 'todo' or task.get('completed') is False:
        return True
    values = [timestamp(task.get(k)) for k in ('created_at', 'updated_at', 'completed_at')]
    if not any(values):
        raise StoreError('任务缺少可核对的时间和状态，不能判定阶段范围。')
    return max(values) >= since


def pages(read, args, max_pages=500):
    items, token, seen = [], '', set()
    for _ in range(max_pages):
        data = read([*args, *(['--page-token', token] if token else [])])
        if not isinstance(data, dict) or not isinstance(data.get('items'), list) or type(data.get('has_more')) is not bool:
            raise StoreError('飞书任务分页响应不完整。')
        items.extend(data['items'])
        if not data['has_more']:
            return items
        token = data.get('page_token')
        if not isinstance(token, str) or not token or token in seen:
            raise StoreError('飞书任务分页重复或缺失，不能宣称完整读取。')
        seen.add(token)
    raise StoreError('飞书任务分页超出本轮上限，不能宣称完整读取。')


def safe_task(task, expected):
    if not isinstance(task, dict) or guid(task.get('guid')) != guid(expected):
        raise StoreError('返回的飞书任务身份不符。')
    if not isinstance(task.get('summary'), str) or not task['summary'].strip() or task.get('status') not in ('todo', 'done'):
        raise StoreError('飞书任务标题或状态缺失。')
    url = task.get('url', '')
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ('applink.feishu.cn', 'applink.larksuite.com')
            or parsed.path not in ('/client/todo/detail', '/client/todo/task')
            or parse_qs(parsed.query).get('guid') != [expected]):
        raise StoreError('飞书任务链接与任务身份不符。')
    result = {k: deepcopy(task[k]) for k in (
        'guid', 'summary', 'description', 'status', 'created_at', 'updated_at', 'completed_at',
        'creator', 'members', 'due', 'start', 'mode', 'source', 'tasklists', 'parent_task_guid',
        'subtask_count', 'repeat_rule', 'next_task_guid', 'dependencies', 'is_milestone') if k in task}
    result['url'] = url
    for key in ('summary', 'description'):
        result[key] = visible_text(result.get(key, ''), 'feishu')
    for key in ('created_at', 'updated_at', 'completed_at'):
        result[key] = timestamp(result.get(key))
    if not result['created_at'] or not result['updated_at'] or (result['status'] == 'done' and not result['completed_at']):
        raise StoreError('任务快照缺少创建、更新或完成时间。')
    if result['status'] == 'todo' and result['completed_at']:
        raise StoreError('任务完成时间与状态冲突，请重新读取稳定版本。')
    for key in ('due', 'start'):
        if result.get(key):
            value = result[key]
            if not isinstance(value, dict) or type(value.get('is_all_day')) is not bool:
                raise StoreError('任务起止时间格式无效。')
            stamp = timestamp(value.get('timestamp'))
            if not stamp:
                raise StoreError('任务起止日期缺失。')
            # All-day timestamps encode a calendar date, not a promised 08:00 deadline.
            result[key] = {'value': stamp[:10] if value['is_all_day'] else stamp, 'is_all_day': value['is_all_day']}
    for key in ('members', 'tasklists', 'dependencies'):
        values = result.get(key, [])
        if not isinstance(values, list) or any(not isinstance(x, dict) for x in values):
            raise StoreError('任务成员、清单或依赖格式不完整。')
        if key == 'members':
            values = [{k: x[k] for k in ('id','type','role','name') if k in x} for x in values]
        result[key] = sorted(values, key=lambda x: str(sorted(x.items())))
    return result


def task_record(task, source_id):
    state = '已完成' if task['status'] == 'done' else '未完成'
    lines = ['任务：' + task['summary'], '飞书当前状态：' + state]
    if task.get('description'):
        lines.append('任务说明：' + task['description'])
    for key, label in (('start','开始'), ('due','截止')):
        if task.get(key):
            lines.append(label + ('日期：' if task[key]['is_all_day'] else '时间：') + task[key]['value'])
    for key, label in (('created_at','创建时间'),('updated_at','最后更新时间'),('completed_at','完成时间')):
        if task.get(key):
            lines.append(label + '：' + task[key])
    members = task.get('members', [])
    for role, label in (('assignee','负责人'), ('follower','关注人')):
        people = [m for m in members if m.get('role') == role]
        if people:
            names = [m.get('name') for m in people if m.get('name')]
            lines.append(label + '：' + ('、'.join(names) if len(names) == len(people) else str(len(people)) + '人，按账号关联核实名单'))
    if task.get('parent_task_guid'):
        lines.append('本项为子任务；完成本项不代表父任务或整个项目完成。')
    if task.get('subtask_count'):
        lines.append('子任务数量：' + str(task['subtask_count']))
    return dict(source_id=source_id, object_id='feishu-task:' + task['guid'], kind='task',
        title=task['summary'], body='\n'.join(lines), url=task['url'], sender='',
        event_at=task['updated_at'], task=task, content_mode='task_snapshot')


def collect_task(run, read=lark_read):
    identity = run['source']['identity']
    data = read(['task','tasks','get','--task-guid',identity])
    task = safe_task(data.get('task'), identity)
    return [task_record(task, run['source']['id'])], dict(complete=True, pages=1,
        coverage='此任务当前可读快照；含状态、文字、分工和起止时间，不含附件或历史操作记录。')


def discover(raw, read=lark_read, register=False, since=SINCE):
    require_feishu(discovery=True)
    if dt.datetime.fromisoformat(since) < dt.datetime.fromisoformat(SINCE):
        raise StoreError('已完成任务的历史发现不得早于本机已确认的历史起点。')
    found, lists, errors, protected, excluded, entries = {}, {}, [], [], [], []
    with raw.store.connect() as c:
        known = {r['identity']: dict(r) for r in c.execute("SELECT * FROM raw_sources WHERE platform='feishu' AND kind='task'")}
    def error(stage, exc, identity=''):
        errors.append(dict(stage=stage, identity=identity, reason=str(exc)))
    def add(items):
        for item in items:
            identity = guid(item.get('guid'))
            found[identity] = item
            for entry in item.get('tasklists') or []:
                lid = guid(entry.get('tasklist_guid'))
                lists.setdefault(lid, {'guid': lid})
    for label, args in (
        ('与我相关任务', ['task','+get-related-tasks','--include-complete','--page-limit','40']),
        ('我负责的任务', ['task','+get-my-tasks','--page-limit','40'])):
        try: add(pages(read, args))
        except (StoreError, OSError) as exc: error(label, exc)
    try:
        for item in pages(read, ['task','tasklists','list','--page-size','100']):
            lists[guid(item.get('guid'))] = item
    except (StoreError, OSError) as exc: error('可见清单', exc)
    visited_lists = set()
    while set(lists) - visited_lists:
        lid = sorted(set(lists) - visited_lists)[0]; visited_lists.add(lid)
        if len(visited_lists) > 500:
            error('清单发现', StoreError('超出清单上限，未完整发现。')); break
        try: add(pages(read, ['task','tasklists','tasks','--tasklist-guid',lid,'--page-size','100']))
        except (StoreError, OSError) as exc: error('清单任务', exc, lid)
    visited = set()
    while set(found) - visited:
        identity = sorted(set(found) - visited)[0]; visited.add(identity)
        if identity in known and known[identity]['state'] != 'approved':
            protected.append(identity); continue
        item = found[identity]
        try:
            if identity not in known and not in_scope(item, since):
                excluded.append(dict(guid=identity, reason='阶段前已完成，未归档正文')); continue
            task = safe_task(read(['task','tasks','get','--task-guid',identity]).get('task'), identity)
            if task.get('subtask_count'):
                add(pages(read, ['task','subtasks','list','--task-guid',identity,'--page-size','100']))
            if identity not in known and not in_scope(task, since):
                excluded.append(dict(guid=identity, reason='详情核对为阶段前已完成')); continue
            entry = dict(identity=identity, name=task['summary'], url=task['url'], since=since,
                basis={'note':'用户要求接入飞书任务并补齐现有任务；当前用户身份可读取。',
                       'evidence':task['url']}, metadata={'created_at':task['created_at'], 'status':task['status']})
            entries.append(entry)
        except (StoreError, OSError) as exc: error('任务详情', exc, identity)
    registered = [raw.register_work_task(**entry) for entry in entries] if register else []
    return dict(complete=not errors, discovered_tasks=len(found), in_scope=len(entries),
        tasklists=len(lists), visited_tasklists=len(visited_lists), errors=errors, protected=protected,
        excluded=excluded, entries=entries, added_sources=sum(not x.get('unchanged') for x in registered),
        registered=[x['id'] for x in registered])
