"""Derived personal relevance for every business list; never writes role assignments."""
import re

from review_requirements import known


def build(records, meta, people_index):
    catalog = {kind:{r['id']:r for r in rows if not r.get('_removed')} for kind,rows in records.items()}
    result = {kind:{rid:{'rank':3,'reason':'暂未建立与你的明确关联'} for rid in rows} for kind,rows in catalog.items()}
    me = meta.get('current_user_id')
    if not me or me not in catalog['people']:
        return result
    followed = set(meta.get('work_focus',{}).get('followed_person_ids') or [])

    def put(kind, rid, rank, reason):
        if rid in result.get(kind,{}) and rank < result[kind][rid]['rank']:
            result[kind][rid] = {'rank':rank,'reason':reason}

    def person_ids(record):
        return {*(record.get('participant_ids') or [] if known(record,'participant_ids') else []),
                *(record.get('contact_person_ids') or [] if known(record,'contact_person_ids') else []),
                record.get('owner_id') if known(record,'owner_id') else None,
                record.get('requester_id') if known(record,'requester_id') else None} - {None,''}

    def valid_roles(pid):
        return [r for r in people_index.get(pid,{}).get('project_roles',[]) if r.get('state') not in ('pending','rejected') and r.get('label') != '工作群沟通参与者']

    own_roles = {r['target_id'] for r in valid_roles(me)}
    own_departments = {r['id'] for r in records['departments'] if not r.get('archived') and known(r,'owner_id') and r.get('owner_id') == me}
    my_directory = {r['id'] for r in people_index.get(me,{}).get('departments',[]) if r.get('state') not in ('pending','rejected')}
    my_orgs = set(people_index.get(me,{}).get('organization_ids') or [])
    if catalog['people'][me].get('organization_id'):
        my_orgs.add(catalog['people'][me]['organization_id'])
    put('people',me,0,'本人')
    for rid in own_departments:
        put('departments',rid,0,'你负责的部门')
    for rid in my_directory:
        put('departments',rid,1,'你的部门归属或目录登记')
    for rid in my_orgs:
        put('organizations',rid,0,'你的所属组织')
    for rid in followed:
        put('people',rid,1,'你关注的人员')
    for person in records['people']:
        if known(person,'manager_id') and person.get('manager_id') == me:
            put('people',person['id'],0,'明确向你汇报')
        departments = {r['id'] for r in people_index.get(person['id'],{}).get('departments',[]) if r.get('state') not in ('pending','rejected')}
        if departments & own_departments:
            put('people',person['id'],1,'与你负责的部门相关')
        elif set(people_index.get(person['id'],{}).get('organization_ids') or []) & my_orgs:
            put('people',person['id'],2,'同一组织的人员')

    limited_projects = set()
    for project in records['projects']:
        rid = project['id']
        if project.get('archived'):
            continue
        role = project.get('my_role') or ''
        role_known = known(project,'my_role')
        limited = role_known and re.search(r'仅关注|只关注|目前关注|暂时关注|先关注|暂不承担|不参与|不再参与|不新增.*任务',role)
        if limited:
            limited_projects.add(rid)
            if '关注' in role:
                put('projects',rid,1,'你已明确仅关注该项目')
            # An explicit current boundary takes precedence over an old participation relation.
            continue
        if me in person_ids(project) or rid in own_roles or role_known:
            put('projects',rid,0,'你负责或有明确参与分工')
        elif person_ids(project) & followed:
            put('projects',rid,1,'你关注的人员参与')

    direct_projects = {rid for rid,score in result['projects'].items() if score['rank'] == 0 and not catalog['projects'][rid].get('archived')}
    for task in records['tasks']:
        if task.get('archived'):
            continue
        people = person_ids(task)
        if me in people:
            put('tasks',task['id'],0,'你负责、参与或对接的工作')
            if task.get('project_id') and task['project_id'] not in limited_projects:
                put('projects',task['project_id'],0,'有你明确承接或参与的工作')
                direct_projects.add(task['project_id'])
        elif task.get('project_id') in direct_projects:
            put('tasks',task['id'],1,'属于你参与的项目')
        elif people & followed:
            put('tasks',task['id'],1,'你关注的人员参与')
        elif set(task.get('participating_department_ids') or []) & own_departments:
            put('tasks',task['id'],1,'与你负责的部门相关')

    # Re-evaluate project association after all directly assigned work has been seen.
    for task in records['tasks']:
        if not task.get('archived') and task.get('project_id') in direct_projects:
            put('tasks',task['id'],1,'属于你参与的项目')
    org_fields = ('customer_ids','channel_ids','partner_ids','supplier_ids','implementation_org_ids','operating_org_ids','internal_org_ids')
    related_capabilities = set()
    related_demands = set()
    related_roadmap = set()
    for rid in direct_projects:
        project = catalog['projects'].get(rid,{})
        for pid in person_ids(project):
            put('people',pid,1,'与你在同一项目协作')
        for field in org_fields:
            for oid in project.get(field) or []:
                put('organizations',oid,1,'与你参与的项目有关')
        for did in project.get('participating_department_ids') or []:
            put('departments',did,1,'与你参与的项目有关')
        related_capabilities.update(project.get('capability_ids') or [])
        related_demands.update(project.get('demand_ids') or [])
    for person in records['people']:
        if any(role['target_id'] in direct_projects for role in valid_roles(person['id'])):
            put('people',person['id'],1,'与你在同一项目有明确分工')
    for task in records['tasks']:
        if task.get('archived') or result['tasks'][task['id']]['rank'] > 1:
            continue
        for pid in person_ids(task):
            put('people',pid,1,'与你相关的工作有明确关联')
        if task.get('organization_id'):
            put('organizations',task['organization_id'],1,'与你相关的工作有关')
        related_demands.update(v for v in [task.get('demand_id')] if v)
        related_roadmap.update(v for v in [task.get('roadmap_id')] if v)
    for kind in ('organizations','departments','roadmap','demands'):
        for record in records[kind]:
            if record.get('archived'):
                continue
            if (known(record,'owner_id') and record.get('owner_id') == me
                    or known(record,'requester_id') and record.get('requester_id') == me):
                put(kind,record['id'],0,'你明确负责或提出')
            elif record.get('project_id') in direct_projects:
                put(kind,record['id'],1,'与你参与的项目有关')
    for rid in related_demands:
        put('demands',rid,1,'与你的项目或工作有关')
        demand = catalog['demands'].get(rid,{})
        related_capabilities.update(v for v in [demand.get('capability_id')] if v)
        related_roadmap.update(v for v in [demand.get('roadmap_id')] if v)
    for rid in related_roadmap:
        put('roadmap',rid,1,'与你的项目或工作有关')
    for rid in related_capabilities:
        put('capabilities',rid,1,'与你的项目或工作有关')
    for account in records.get('contacts',[]):
        score = result['people'].get(account.get('person_id'))
        if score:
            result['contacts'][account['id']] = dict(score)
    result['work_preferences'] = {'work-focus':{'rank':0,'reason':'你的关注设置'}}
    return result


def score(index, kind, rid):
    return index.get(kind,{}).get(rid,{}).get('rank',3)


def question_score(index, card):
    return min((score(index,target['kind'],target['id']) for target in card['targets']),default=3)


def source_index(conn, records, relevance):
    """Use saved linkage metadata only; never inspect a message body to decide scope."""
    import json
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='raw_sources'").fetchone():
        return {}
    identities = {}
    for person in records['people']:
        if person.get('archived'):
            continue
        for account in person.get('accounts') or []:
            if account.get('archived'):
                continue
            for field in ('account_id','account_alias'):
                if account.get(field):
                    identities.setdefault(account[field],set()).add(person['id'])
    result = {}
    for source in conn.execute('SELECT id,identity,config FROM raw_sources'):
        config = json.loads(source['config'])
        ranks = [score(relevance,'projects',rid) for rid in config.get('projects',[])]
        people = identities.get(source['identity'],set())
        if len(people) == 1:
            ranks.extend(score(relevance,'people',pid) for pid in people)
        result[source['id']] = min(ranks,default=3)
    return result
