'use strict';
// Account views resolve current relationships from the linked person's dossier.
// Candidate identities and pending roles must never become asserted account facts.
function accountContext(account){
 const p=data.records.people.find(p=>p.id===account.person_id&&!p.archived);
 const s=p?(data.people_index||{})[p.id]||{}:{};
 const organizations=[account.organization,...(s.organization_ids||[]).map(id=>data.records.organizations.find(o=>o.id===id&&!o.archived)?.name)].filter(Boolean);
 const projects=[...(account.project_ids||[]),...(p?.project_ids||[]),...(s.project_roles||[]).filter(r=>r.state!=='pending'&&r.state!=='rejected').map(r=>r.target_id),...data.records.projects.filter(q=>p&&(q.owner_id===p.id||(q.participant_ids||[]).includes(p.id))).map(q=>q.id)].filter(id=>data.records.projects.some(q=>q.id===id&&!q.archived));
 return {organizations:[...new Set(organizations)],projects:[...new Set(projects)],person_id:p?.id||''};
}
function accountContextHTML(account){
 const s=accountContext(account);
 return `${escape(s.organizations.join('、')||'组织资料仍缺失')}<span class="subline">${escape(s.projects.map(projectName).join('、')||'暂无项目资料')}</span>${s.person_id?`<a class="subline" href="#people/${encodeURIComponent(s.person_id)}">查看人员档案与依据 →</a>`:''}`;
}
const relationLabels = {owner_id:'负责人',participant_ids:'参与人员',manager_id:'直属上级',requester_id:'需求提出人',person_id:'已确认人员',candidate_person_id:'候选人员（未确认）',organization_id:'所属单位',parent_org_id:'上级单位',customer_ids:'客户单位',channel_ids:'渠道单位',partner_ids:'合作单位',project_id:'所属项目',project_ids:'参与项目',capability_id:'关联能力',capability_ids:'关联能力',roadmap_id:'产品路线',demand_id:'需求'};
const relationTargets = {owner_id:['people',false],participant_ids:['people',true],manager_id:['people',false],requester_id:['people',false],person_id:['people',false],candidate_person_id:['people',false],organization_id:['organizations',false],parent_org_id:['organizations',false],customer_ids:['organizations',true],channel_ids:['organizations',true],partner_ids:['organizations',true],project_id:['projects',false],project_ids:['projects',true],capability_id:['capabilities',false],capability_ids:['capabilities',true],roadmap_id:['roadmap',false],demand_id:['demands',false]};
Object.assign(relationLabels,{contact_ids:'相关工作账号',demand_ids:'产品需求'});
Object.assign(relationTargets,{contact_ids:['contacts',true],demand_ids:['demands',true]});
Object.assign(relationLabels,{parent_department_id:'上级部门',participating_department_ids:'参与部门',supplier_ids:'设备 / 服务供应方',implementation_org_ids:'实施交付方',operating_org_ids:'运营运维方',internal_org_ids:'内部参与组织'});
Object.assign(relationTargets,{parent_department_id:['departments',false],participating_department_ids:['departments',true],supplier_ids:['organizations',true],implementation_org_ids:['organizations',true],operating_org_ids:['organizations',true],internal_org_ids:['organizations',true]});
const extraLabels = {people:'人员与账号',organizations:'往来单位',primary_type:'主类型',work_types:'协作类型',roles:'业务角色',legal_name:'组织全称（资料署名）',work_scope:'当前负责范围',business_value:'商业价值 / 复制价值',decision_needed:'需要拍板的事项',mapping_note:'账号对应依据',task_type:'事项类型',parent_org_id:'上级单位'};
function relationSpec(key){return [key,relationLabels[key],'relation'];}
function extraField(spec,value,attrs){
  const [key,,type]=spec;let options=[],multiple=false;
  if(type==='relation'){
    const [target,multi]=relationTargets[key];multiple=multi;
    options=data.records[target].filter(r=>(!r.archived||r.id===value||Array.isArray(value)&&value.includes(r.id))&&!(target===dialogKind&&r.id===detail.record.id)).map(r=>[r.id,titleOf(r)]);
  }else if(type==='taxonomy'){
    const values={primary_type:data.taxonomy.project_types,work_types:data.taxonomy.project_types,roles:data.taxonomy.org_roles,task_type:data.taxonomy.task_types,support_mode:data.taxonomy.project_modes,decision:['同意建议','调整后推进','暂缓','不做']}[key];
    options=(key==='unit_type'?data.taxonomy.department_types:values).map(v=>[v,v]);multiple=['roles','work_types'].includes(key);
  }else return null;
  if(!multiple)options.unshift(['','未设置']);
  return `<select ${attrs} ${multiple?'multiple':''}>${options.map(([v,n])=>`<option value="${escape(v)}" ${(Array.isArray(value)?value.includes(v):value===v)?'selected':''}>${escape(n)}</option>`).join('')}</select>${multiple?'<small class="form-hint">可多选；按住 Ctrl 选择多个。</small>':''}`;
}
function relationName(key,value){const target=relationTargets[key]?.[0];if(!target)return String(value??'');if(Array.isArray(value))return value.map(id=>titleOf(findRecord(target,id)||{name:'已不可用的关联'})).join('、');return value?titleOf(findRecord(target,value)||{name:'已不可用的关联'}):'未关联';}
function relationshipsHTML(){
  const rows=(detail.related||[]).filter(r=>r.kind!=='updates'&&r.kind!=='contacts').sort(compareLinked);
  if(!rows.length)return '<p class="notice">尚无关联记录。可以在工作中关联项目、人员或单位。</p>';
  return `<div class="section-title"><h3>关联记录</h3><span class="muted">双向查看 · 不重复建档</span></div><div class="relationship-list">${rows.map(r=>`<div class="relationship-row"><span class="muted">${r.direction==='in'?'作为其':''}${escape(relationLabels[r.relation]||r.relation)}</span>${link(r.kind,{id:r.id,name:r.name,archived:r.archived})}<small>${escape(label[r.kind])}</small></div>`).join('')}</div>`;
}
function responsibilityPanels(){
 const tasks=data.records.tasks.filter(r=>!r.archived&&!workClosed(r)).sort(compareWork);
 const questions=data.judgment_items||[],count=data.judgments?.unresolved||0;
 const decisions=panel('待你判断',`<div class="panel-body"><p><strong>${count} 个具体问题</strong>，其中 ${data.judgments?.deferred||0} 个稍后处理。</p>${questions.map(c=>`<div class="recent-row"><a href="#reviews/${encodeURIComponent(c.id)}">${escape(c.question)}</a><small>${c.state==='deferred'?'稍后处理，仍未解决':'有背景、依据与建议，可直接确认或修改'}</small></div>`).join('')||'<p class="muted">当前没有未解决的问题。</p>'}</div>`,`<a href="#reviews">打开问题 →</a>`);
 const dates=tasks.filter(r=>r.due_date).sort((a,b)=>compareRelevant('tasks',a,b)||a.due_date.localeCompare(b.due_date));
 const due=panel('有明确日期的工作',`<div class="panel-body">${dates.slice(0,5).map(r=>`<div class="recent-row">${link('tasks',r)}<small>${escape(r.due_date)} · ${escape(r.owner_id?relationName('owner_id',r.owner_id):r.owner||'承接人未明确')}</small></div>`).join('')||'<p class="muted">当前没有登记明确截止日期。</p>'}<p class="muted">历史安排未记结果时保持原状，不自动认定逾期。</p></div>`);
 const urgent=panel('优先关注的工作',workCards('tasks',tasks.filter(r=>r.priority==='高').slice(0,3)),`<a href="#tasks">查看所有工作 →</a>`);
 return `<div class="overview-bottom">${decisions}${due}</div><div class="overview-work">${urgent}</div>`;
}
