'use strict';
const workScopeLabels={mine:'我的工作',following:'关注人的工作',others:'其他人的工作',unassigned:'人员未明确',all:'全部工作'};
const workFocus = () => data.meta.work_focus||{followed_person_ids:[],revision:0};
const followedPeople = () => workFocus().followed_person_ids.map(id=>findRecord('people',id)).filter(Boolean);
function selectedWorkScope(){const key=location.hash.split('/')[1];return Object.hasOwn(workScopeLabels,key)?key:'mine';}
function workGroup(r){
 const people=[r.owner_id,...(r.participant_ids||[])].filter(Boolean),me=data.meta.current_user_id;
 if(me&&people.includes(me))return 'mine';
 if(people.some(id=>workFocus().followed_person_ids.includes(id)))return 'following';
 return people.length||r.owner?.trim()?'others':'unassigned';
}
function workScopeDescription(){
 const names=followedPeople().map(p=>p.name).join('、');
 return {mine:`${findRecord('people',data.meta.current_user_id)?.name||'本人'}负责或参与的工作。`,following:names?`${names}负责或参与的工作；你也参与的事项已列入“我的工作”。`:'尚未设置关注人，可在上方添加。',others:'其他人员负责或参与的工作；已出现在前两类的事项不再重复。',unassigned:'现有资料尚未明确负责人或参与人，保留在这里，后续可按实际情况补充。',all:'查看所有已记录的工作，包括人员尚未明确的事项。'}[selectedWorkScope()];
}
function renderWorkFocus(rows){
 const selected=selectedWorkScope(),counts={mine:0,following:0,others:0,unassigned:0,all:rows.length};
 for(const r of rows)counts[workGroup(r)]++;
 $('work-focus').innerHTML=`<div class="work-focus-tabs" role="group" aria-label="按人员查看工作">${Object.entries(workScopeLabels).map(([key,name])=>`<button type="button" data-action="work-scope" data-scope="${key}" aria-pressed="${key===selected}" class="${key===selected?'active':''}">${name}<span>${counts[key]}</span></button>`).join('')}</div><div class="work-focus-people"><p><strong>关注人</strong> ${escape(followedPeople().map(p=>p.name).join('、')||'尚未设置')}</p><button class="table-action" type="button" data-action="work-focus-edit">设置关注人</button></div>`;
}
let workFocusRevision=0,workFocusSaving=false;
function showWorkFocus(){
 workFocusRevision=workFocus().revision;
 const followed=workFocus().followed_person_ids,selected=new Set(followed),people=data.records.people.filter(p=>!p.archived&&p.id!==data.meta.current_user_id).sort((a,b)=>Number(selected.has(b.id))-Number(selected.has(a.id))||(selected.has(a.id)&&selected.has(b.id)?followed.indexOf(a.id)-followed.indexOf(b.id):compareRelevant('people',a,b)||a.name.localeCompare(b.name,'zh-CN')));
 $('work-focus-form-body').innerHTML=`<p class="form-hint">选择需要集中查看工作的人。本人工作单独展示；调整关注人不会改变工作分工。</p><label class="form-field">查找人员<input type="search" id="work-focus-search" placeholder="搜索姓名或组织"></label><fieldset class="work-focus-options"><legend class="sr-only">关注人员</legend>${people.map(p=>{const org=findRecord('organizations',p.organization_id)?.name||'',caption=p.name+' '+org;return `<label data-focus-person data-search="${escape(caption.toLowerCase())}"><input type="checkbox" name="followed_person_ids" value="${escape(p.id)}" ${selected.has(p.id)?'checked':''}><span>${escape(p.name)}<small>${escape(org)}</small></span></label>`;}).join('')}</fieldset><p id="work-focus-empty" class="muted" hidden>没有匹配的人员。</p><p id="work-focus-count" class="form-hint"></p><div id="work-focus-error" class="form-error" role="alert" hidden></div><button type="button" id="work-focus-reload" class="table-action" data-action="work-focus-reload" hidden>重新读取当前名单</button><div class="form-footer"><button type="button" class="secondary-button" data-action="work-focus-close">取消</button><button type="submit" class="primary-button">保存关注人</button></div>`;
 updateWorkFocusCount();if(!$('work-focus-dialog').open)$('work-focus-dialog').showModal();$('work-focus-search').focus();
}
function updateWorkFocusCount(){$('work-focus-count').textContent=`已选 ${document.querySelectorAll('#work-focus-form input[name="followed_person_ids"]:checked').length} 人`;}
function filterWorkFocus(){const q=$('work-focus-search').value.trim().toLowerCase();let visible=0;document.querySelectorAll('[data-focus-person]').forEach(row=>{row.hidden=!row.dataset.search.includes(q);if(!row.hidden)visible++;});$('work-focus-empty').hidden=visible>0;}
async function saveWorkFocus(event){
 event.preventDefault();if(workFocusSaving)return;workFocusSaving=true;const form=event.target,button=form.querySelector('[type="submit"]');button.disabled=true;$('work-focus-error').hidden=true;
 try{const followed_person_ids=new FormData(form).getAll('followed_person_ids');const saved=await api('/api/work/focus',{revision:workFocusRevision,followed_person_ids});data.meta.work_focus=saved;$('work-focus-dialog').close();await load();toast('关注人已保存，工作分类已更新。');}
 catch(error){$('work-focus-error').hidden=false;$('work-focus-error').textContent=error.message;$('work-focus-reload').hidden=false;}
 finally{workFocusSaving=false;button.disabled=false;}
}
async function workFocusAction(button){
 const a=button.dataset.action;
 if(a==='work-scope'){history.replaceState(null,'','#tasks/'+button.dataset.scope);await render();document.querySelector(`[data-action="work-scope"][data-scope="${selectedWorkScope()}"]`)?.focus();return;}
 if(a==='work-focus-edit')return showWorkFocus();
 if(a==='work-focus-close')return $('work-focus-dialog').close();
 if(a==='work-focus-reload'){if(!await confirmAction('重新读取会放弃当前未保存的名单选择，是否继续？'))return;await load();showWorkFocus();}
}
document.addEventListener('input',event=>{if(event.target.id==='work-focus-search')filterWorkFocus();});
document.addEventListener('change',event=>{if(event.target.matches('#work-focus-form input[type="checkbox"]'))updateWorkFocusCount();});
