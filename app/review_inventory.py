"""Read-only distinction between actionable responsibility gaps and optional blanks."""
from judgment_cards import JudgmentCards
from review_requirements import known, requirements


def in_connection(store, conn, records=None, cards=None, people_index=None):
    if records is None:
        kinds = ('projects','tasks','people','organizations','departments','roadmap','demands')
        records = {kind:list(store._records(conn, kind).values()) for kind in kinds}
    rows = {kind:[r for r in values if not r.get('archived')] for kind,values in records.items()}
    cards = JudgmentCards(store).queue_in_connection(conn) if cards is None else cards
    essential = []

    def add(kind, record, label, fields):
        if any(known(record, f) for f in fields):
            return
        covered = [c['id'] for c in cards if c['state'] in ('pending','deferred') and any(
            t['kind'] == kind and t['id'] == record['id'] and set(t['values']) & set(fields) for t in c['targets'])]
        protected = [c['id'] for c in cards if c['state'] in ('resolved','dismissed') and any(
            v['kind'] == kind and v['id'] == record['id'] and set(v['any_of']) & set(fields) for v in requirements(c))]
        essential.append({'kind':kind,'id':record['id'],'name':record.get('name') or record.get('title'),
                          'label':label,'fields':fields,'questions':covered,'protected_history':protected,
                          'uncovered':not covered and not protected})

    for record in rows['projects']:
        add('projects',record,'本阶段负责人',['owner_id','owner'])
        add('projects',record,'我的参与方式',['my_role'])
    for record in rows['tasks']:
        if record['state'] not in ('已完成','已取消','暂缓'):
            add('tasks',record,'承接或协作分工',['owner_id','owner','participant_ids'])

    observations = []
    def observe(kind, label, predicate, explanation):
        missing = [{'id':r['id'],'name':r.get('name') or r.get('title')} for r in rows[kind] if predicate(r)]
        observations.append({'kind':kind,'label':label,'count':len(missing),'explanation':explanation,'items':missing})

    observe('projects','项目阶段待核实',lambda r:not known(r,'stage'),'尚未记录或仍为整理建议；结合具体进展核实，不从空白推定未开始。')
    observe('tasks','优先级未设置',lambda r:not known(r,'priority'),'没有明确优先安排可以留空；需要取舍时再提出具体问题。')
    observe('tasks','未设明确截止',lambda r:not known(r,'due_date'),'没有约定日期不强制补日期，也不推定逾期。')
    observe('tasks','下一步未单独记录',lambda r:r['state'] not in ('已完成','已取消') and not known(r,'next_action'),'结合工作过程与等反馈状态核对；不把每项空白都当作新安排。')
    observe('tasks','已结束工作未关联承接人员',lambda r:r['state'] in ('已完成','已取消') and not any(known(r,f) for f in ('owner_id','owner','participant_ids')),'保留历史结果，能核实就补；不为补名字重新安排已经结束的工作。')
    observe('people','职务名称未记录',lambda r:not known(r,'role'),'已知项目分工与正式职务分开；工作联系人不一定需要完整职务。')
    observe('people','所属组织未记录',lambda r:not known(r,'organization_id') and not (people_index or {}).get(r['id'],{}).get('organization_ids'),'有明确身份依据再补；需要明确代表哪方时提出具体问题。')
    observe('people','平台账号未关联',lambda r:not r.get('accounts'),'会议或文档认识的人可以先建档，不凭同名猜账号。')
    observe('organizations','业务角色尚未明确',lambda r:not r.get('roles'),'工作联系不自动等同客户、供应商或合作伙伴。')
    observe('organizations','单位负责人未记录',lambda r:not known(r,'owner_id'),'已有业务对接关系不要求知道单位最高负责人。')
    observe('departments','部门负责人未记录',lambda r:not known(r,'owner_id'),'目录分组和实际部门区分，不为目录分组指派负责人。')
    observe('roadmap','方向尚未指派负责人',lambda r:not any(known(r,f) for f in ('owner_id','owner')),'路线方向不等于已批准立项；发生实施安排时再核对。')
    observe('demands','产品需求尚未指派负责人',lambda r:not any(known(r,f) for f in ('owner_id','owner')),'产品需求本阶段只读，保留待后续讨论。')
    return {'essential':essential,'summary':{'essential_fields':len(essential),
            'essential_records':len({(r['kind'],r['id']) for r in essential}),
            'uncovered_fields':sum(r['uncovered'] for r in essential),
            'uncovered_records':len({(r['kind'],r['id']) for r in essential if r['uncovered']}),
            'protected_fields':sum(bool(r['protected_history']) and not r['questions'] for r in essential)},
            'observations':observations}


def status(store):
    from people import People
    with store.connect() as conn:
        conn.execute('BEGIN')
        return in_connection(store,conn,people_index=People(store).index(conn))
