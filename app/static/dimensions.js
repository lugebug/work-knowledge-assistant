'use strict';
function installDimensionSpecs(){
 Object.assign(label,{departments:'组织内部门'});
 Object.assign(fieldLabels,{aliases:'其他称呼',organization_type:'组织类型',region:'服务区域',unit_type:'部门类型',location:'项目地点'});
 specs.people.splice(1,0,['aliases','其他姓名 / 称呼']);
 specs.organizations.splice(1,0,['aliases','其他名称'],['organization_type','组织类型'],['region','服务区域']);
 specs.departments=[['name','部门名称','text',true],relationSpec('organization_id'),['unit_type','部门类型','taxonomy'],['state','状态','state'],relationSpec('parent_department_id'),relationSpec('owner_id'),['description','职责范围','textarea'],['aliases','其他名称'],['notes','补充说明','textarea']];
}
function departmentsList(){
 const rows=visibleRows('departments');$('result-count').textContent=`${rows.length} 个部门 / 分组`;
 return `<p class="notice">实际部门、业务线与目录分组分别记录。负责人有明确依据时展示，未知时保留空白。</p><section class="panel"><div class="table-wrap"><table><thead><tr><th>部门</th><th>所属组织 / 上级部门</th><th>负责人</th><th>职责与依据</th><th>操作</th></tr></thead><tbody>${rows.map(r=>`<tr><td class="name-cell">${link('departments',r)}<span class="subline">${escape(r.unit_type||'待分类')} · ${escape(r.state)}</span></td><td>${escape(relationName('organization_id',r.organization_id))}<span class="subline">${escape(r.parent_department_id?relationName('parent_department_id',r.parent_department_id):'未登记上级部门')}</span></td><td>${escape(r.owner_id?relationName('owner_id',r.owner_id):'暂无明确资料')}</td><td>${escape(r.description||'资料待补')}</td><td><button class="table-action" data-action="edit" data-kind="departments" data-id="${escape(r.id)}">维护</button></td></tr>`).join('')}</tbody></table></div>${rows.length?'':empty('没有符合条件的部门','可以调整筛选，或新建部门。','departments')}</section>`;
}
function relationDimensionSummary(r){
 const fields=[r.represented_org_name&&'代表组织：'+r.represented_org_name,r.department_name&&'代表部门：'+r.department_name,r.phase&&'适用阶段：'+r.phase,(r.valid_from||r.valid_to)&&'适用时间：'+(r.valid_from||'未设起始')+' 至 '+(r.valid_to||'未设结束')].filter(Boolean);
 return fields.length?`<p class="muted">${fields.map(escape).join(' · ')}</p>`:'';
}
function relationDimensionFields(r){
 const select=(key,name,kind)=>`<label class="form-field">${name}<select name="${key}"><option value="">未设置</option>${data.records[kind].filter(x=>!x.archived||x.id===r[key]).map(x=>`<option value="${escape(x.id)}" ${x.id===r[key]?'selected':''}>${escape(x.name)}</option>`).join('')}</select></label>`;
 return `<div class="form-grid">${select('represented_org_id','在此关系中代表的组织','organizations')}${select('department_id','在此关系中代表的部门','departments')}<label class="form-field">适用阶段<input name="phase" maxlength="160" value="${escape(r.phase||'')}" placeholder="例如：联调测试、交付、运营"></label><label class="form-field">开始日期<input name="valid_from" type="date" value="${escape(r.valid_from||'')}"></label><label class="form-field">结束日期<input name="valid_to" type="date" value="${escape(r.valid_to||'')}"></label></div>`;
}
function openNewRelation(button){
 const kind=button.dataset.kind||'projects',target=button.dataset.target||'',person=button.dataset.person||'';
 const options=(k,value)=>data.records[k].filter(x=>!x.archived).map(x=>`<option value="${escape(x.id)}" ${x.id===value?'selected':''}>${escape(x.name)}</option>`).join('');
 $('detail-content').innerHTML=`<header class="dialog-header"><h2 id="detail-title">新增人员关系</h2><button class="icon-button" data-action="close-detail" aria-label="关闭">×</button></header><form id="new-relation-form" class="dialog-body"><label class="form-field">人员<select name="person_id" required>${options('people',person)}</select></label><label class="form-field">关联类别<select name="kind" id="new-relation-kind">${[['projects','项目分工'],['organizations','组织任职'],['departments','部门归属 / 管理']].map(([k,n])=>`<option value="${k}" ${k===kind?'selected':''}>${n}</option>`).join('')}</select></label><label class="form-field">关联对象<select name="target_id" id="new-relation-target" required>${options(kind,target)}</select></label><label class="form-field">角色 / 关系<input name="label" required maxlength="120" placeholder="例如：技术对接、部门成员、组织任职"></label><label class="form-field">职责范围<textarea name="detail" maxlength="2000" rows="3"></textarea></label>${relationDimensionFields({})}<p id="new-relation-error" class="form-error" role="alert" hidden></p><div class="form-footer"><button class="primary-button" type="submit">保存关系</button></div></form>`;
 if(!$('detail-dialog').open)$('detail-dialog').showModal();
}
async function saveNewRelation(event){
 event.preventDefault();if(pendingForm)return;pendingForm=true;
 const f=event.target,button=f.querySelector('button[type=submit]');button.disabled=true;
 try{await api('/api/people/create-relation',{values:Object.fromEntries(new FormData(f))});$('detail-dialog').close();await load();toast('已保存人员关系，可在双方档案查看。');}
 catch(err){$('new-relation-error').hidden=false;$('new-relation-error').textContent=err.message;}
 finally{pendingForm=false;button.disabled=false;}
}
document.addEventListener('change',event=>{if(event.target.id==='new-relation-kind')$('new-relation-target').innerHTML=data.records[event.target.value].filter(r=>!r.archived).map(r=>`<option value="${escape(r.id)}">${escape(r.name)}</option>`).join('');});
