"""Record types and validated relationship fields shared by all entry points."""
PROJECT_TYPES = ['直客销售', '渠道销售', '售前', '技术验证', '二次开发', '交付实施', '运营运维', '产品研发', '生态合作']
ORG_ROLES = ['客户', '渠道', '合作伙伴', '设备厂商', '供应商', '内部组织']
KINDS = ('projects', 'people', 'contacts', 'organizations', 'departments', 'capabilities', 'roadmap', 'demands', 'tasks', 'updates')
STATES = {
    'projects': ['进行中', '待启动', '暂缓', '已完成'],
    'people': ['内部同事', '外部人员', '工作联系人', '待核对', '已离开'],
    'contacts': ['待确认', '工作联系人', '非工作联系人'],
    'organizations': ['待核对', '业务往来中', '暂停往来'],
    'departments': ['在用', '目录登记', '规划中', '已停用'],
    'capabilities': ['资料支持，待核对', '特定项目已验证', '需修订资料', '已停用'],
    'roadmap': ['规划方向', '评估中', '已排期', '实施中', '已发布', '暂缓'],
    'demands': ['待评估', '待确认', '已采纳', '实施中', '已完成', '暂缓'],
    'tasks': ['待办', '进行中', '等反馈', '已安排，结果未记录', '已完成', '暂缓', '已取消'],
    'updates': ['资料记录', '已核实', '已修正'],
}
FIELDS = {
    'projects': ['name', 'primary_type', 'work_types', 'state', 'owner', 'owner_id', 'participant_ids', 'customer_ids', 'channel_ids', 'partner_ids', 'stage', 'next_action', 'boundary', 'target_date', 'business_value', 'decision_needed', 'notes', 'archived'],
    'people': ['name', 'organization_id', 'role', 'work_scope', 'manager_id', 'state', 'project_ids', 'notes', 'archived'],
    'contacts': ['name', 'organization', 'organization_id', 'role', 'state', 'person_id', 'candidate_person_id', 'mapping_note', 'project_ids', 'notes', 'archived'],
    'organizations': ['name', 'roles', 'state', 'legal_name', 'owner_id', 'parent_org_id', 'description', 'next_action', 'notes', 'archived'],
    'departments': ['name', 'organization_id', 'parent_department_id', 'owner_id', 'unit_type', 'state', 'description', 'aliases', 'notes', 'archived'],
    'capabilities': ['state', 'notes', 'archived'],
    'roadmap': ['name', 'description', 'state', 'priority', 'owner', 'owner_id', 'target_date', 'capability_ids', 'notes', 'archived'],
    'demands': ['title', 'project_id', 'capability_id', 'roadmap_id', 'organization_id', 'requester_id', 'stage', 'state', 'priority', 'owner', 'owner_id', 'target_date', 'notes', 'archived'],
    'tasks': ['title', 'task_type', 'project_id', 'organization_id', 'demand_id', 'roadmap_id', 'owner', 'owner_id', 'due_date', 'state', 'notes', 'archived'],
}
FIELDS['tasks'] += ['participant_ids', 'participating_department_ids', 'source_date', 'schedule_note', 'progress', 'dependencies']
FIELDS['updates'] = ['title', 'project_id', 'organization_id', 'owner_id', 'participant_ids', 'participating_department_ids', 'source_date', 'description', 'state', 'notes', 'archived']
REQUIRED = {k: 'title' if k in ('demands', 'tasks', 'updates') else 'name' for k in KINDS if k != 'capabilities'}
# One canonical direction per relation; reverse views are derived, never copied.
RELATIONS = {
    'owner_id': ('people', False), 'participant_ids': ('people', True),
    'manager_id': ('people', False), 'requester_id': ('people', False),
    'person_id': ('people', False), 'candidate_person_id': ('people', False),
    'organization_id': ('organizations', False), 'parent_org_id': ('organizations', False),
    'parent_department_id': ('departments', False), 'participating_department_ids': ('departments', True),
    'supplier_ids': ('organizations', True), 'implementation_org_ids': ('organizations', True),
    'operating_org_ids': ('organizations', True), 'internal_org_ids': ('organizations', True),
    'customer_ids': ('organizations', True), 'channel_ids': ('organizations', True), 'partner_ids': ('organizations', True),
    'project_id': ('projects', False), 'project_ids': ('projects', True),
    'capability_id': ('capabilities', False), 'capability_ids': ('capabilities', True),
    'roadmap_id': ('roadmap', False), 'demand_id': ('demands', False),
}
TASK_TYPES = ['一般待办', '售前支持', '待决策', '合作推进', '交付验证']

# All cooperation types share these project dimensions.
PROJECT_MODES = ['直接答复', '关键页方案', '联合评估', '技术验证', '运营支持']
FIELDS['projects'] += ['goal', 'my_role', 'request_summary', 'expected_output', 'contact_ids', 'demand_ids', 'capability_ids', 'requester_id', 'support_mode', 'feasibility', 'cost_estimate', 'quote_reference', 'effort_estimate', 'calendar_estimate', 'resources', 'dependencies', 'milestones', 'progress', 'blockers', 'recommendation', 'decision', 'decision_reason']
RELATIONS.update({'contact_ids': ('contacts', True), 'demand_ids': ('demands', True)})
FIELDS['projects'] += ['supplier_ids', 'implementation_org_ids', 'operating_org_ids', 'internal_org_ids', 'participating_department_ids', 'location']
FIELDS['people'] += ['aliases']
FIELDS['contacts'] += ['nickname', 'remark', 'account_alias']
FIELDS['organizations'] += ['aliases', 'organization_type', 'region']
DEPARTMENT_TYPES = ['实际部门', '业务线', '目录分组', '规划部门']

# Accounts and work history are attributes of the owning business record.
# Legacy kinds remain readable solely for source references and migration history.
FIELDS['people'] += ['accounts']
FIELDS['tasks'] += ['priority', 'background', 'next_action', 'result', 'work_log', 'contact_person_ids']
FIELDS['projects'] += ['context_history', 'priority']
FIELDS['organizations'] += ['context_history']
RELATIONS['contact_person_ids'] = ('people', True)
WORK_PRIORITIES = ['', '高', '普通', '低']
TASK_TYPES += ['方案编写', '沟通联系', '跟进催办', '会议沟通', '评估分析', '问题排查', '资料整理', '执行工作']
