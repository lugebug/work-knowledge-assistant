'use strict';
let reviewQueue=null,reviewFilter='pending',reviewSearch='',reviewSelected=new Set(),reviewDrafts=new Map(),reviewBusy=false;
const reviewCategoryLabels={identity:'身份与账号',organizations:'组织',projects:'项目',departments:'部门',people:'人员',other:'其他资料'};
function reviewCategory(r){return r.kind==='completeness'?(['projects','people','organizations','departments'].includes(r.target_kind)?r.target_kind:'other'):r.kind==='role'?r.target_kind:'identity';}
function judgmentItems(){return (reviewQueue?.items||[]).filter(r=>r.kind==='completeness'||r.kind!=='profile'&&(r.state==='pending'?r.can_confirm:['confirmed','rejected','superseded'].includes(r.state)));}
function reviewRows(){return judgmentItems().filter(r=>(reviewFilter==='done'?r.state!=='pending':r.state==='pending'&&(reviewFilter==='pending'||reviewFilter==='deferred'&&r.kind==='completeness'&&r.dimensions.some(d=>d.state==='deferred')||reviewCategory(r)===reviewFilter))&&(!reviewSearch||[r.title,r.description,r.detail,r.basis,r.note].join(' ').toLowerCase().includes(reviewSearch.toLowerCase())));}
function reviewQuestion(r){
 if(r.kind==='identity')return '这两份档案是否是同一个人？';
 if(r.kind==='account')return '这个聊天账号是否属于这位人员？';
 if(r.target_kind==='projects')return `此人在这个项目中的角色是否是“${r.description}”？`;
 if(r.target_kind==='organizations')return `此人与这个组织的关系是否应记为“${r.description}”？`;
 return `此人与这个部门的关系是否应记为“${r.description}”？`;
}
function reviewNote(r){return reviewDrafts.has(r.key)?reviewDrafts.get(r.key):r.note||'';}
function reviewCard(r,index){
 const active=r.state==='pending',editable=['pending','confirmed','rejected'].includes(r.state),selectable=active&&r.can_confirm,noteId='review-note-'+index;
 return `<article class="review-row" data-review-key="${escape(r.key)}"><header class="review-row-heading"><div class="review-title">${selectable?`<input type="checkbox" data-review-select="${escape(r.key)}" aria-label="选择 ${escape(r.title)}" ${reviewSelected.has(r.key)?'checked':''}>`:'<span class="review-marker" aria-hidden="true">·</span>'}<div><span class="review-type">${escape(reviewCategoryLabels[reviewCategory(r)]||'关系判断')}</span><h2>${escape(r.title)}</h2></div></div>${badge(active?'待你判断':r.state==='confirmed'?'已确认':r.state==='rejected'?'已否定':'已由其他合并处理')}</header>
 <div class="review-body"><div class="review-context"><p class="review-description">${escape(active?reviewQuestion(r):r.description)}</p>${r.detail?`<p>${escape(r.detail)}</p>`:''}<p class="muted">${active?'已有依据与待判断点：':'原线索依据：'}${escape(r.basis||'尚无充分说明，可在相关档案查看已有资料。')}</p><details class="review-evidence"><summary>查看依据与相关档案</summary><p><a href="#people/${encodeURIComponent(r.person_id)}">打开人员档案 →</a>${r.source_id?` · <a href="#people/${encodeURIComponent(r.source_id)}">查看原档案</a>`:''}${r.kind==='role'?` · <button class="record-link" data-action="detail" data-kind="${escape(r.target_kind)}" data-id="${escape(r.target_id)}">查看关联${r.target_kind==='projects'?'项目':r.target_kind==='organizations'?'组织':'部门'}</button>`:''}</p>${personRefs(r.sources)||'<p class="muted">暂无可展开的来源。</p>'}</details></div>
 <div class="review-note"><label for="${noteId}">我的备注</label><textarea id="${noteId}" data-review-note="${escape(r.key)}" rows="2" maxlength="4000" placeholder="例如：真实姓名、所属单位，或这项哪里不对" ${editable?'':'readonly'}>${escape(reviewNote(r))}</textarea><div class="review-actions">${editable?`<button class="quiet-button" data-action="review-note" data-key="${escape(r.key)}">保存备注</button>${r.kind==='profile'?`<button class="secondary-button" data-action="edit" data-kind="people" data-id="${escape(r.person_id)}">补充资料</button>`:r.kind==='role'?`<button class="secondary-button" data-action="review-edit-role" data-key="${escape(r.key)}">修正分工</button>`:r.kind==='account'?`<button class="secondary-button" data-action="edit" data-kind="contacts" data-id="${escape(r.id)}">调整对应</button>`:''}${selectable?`<button class="quiet-button danger-button" data-action="review-reject" data-key="${escape(r.key)}">${r.kind==='identity'?'不是同一人':'不成立'}</button><button class="primary-button" data-action="review-confirm" data-key="${escape(r.key)}">${r.kind==='identity'?'确认同一人':'确认'}</button>`:''}${r.can_undo?`<button class="secondary-button" data-action="review-undo" data-key="${escape(r.key)}">撤销整次合并</button>`:''}`:''}</div></div></div></article>`;
}
function drawReviews(){drawReviewWorkspace();}
async function renderReviews(){
 $('list-tools').hidden=true;$('create-button').hidden=true;
 $('content').innerHTML='<div class="loading-panel" role="status">正在读取待判断关系…</div>';
 try{const result=await api('/api/people/reviews');if(page!=='reviews')return;reviewQueue=result;reviewSelected=new Set([...reviewSelected].filter(key=>result.items.some(r=>r.key===key&&reviewRowSelectable(r))));drawReviews();}
 catch(err){$('content').innerHTML=`<section class="panel empty"><h2>待判断事项暂时无法读取</h2><p>${escape(err.message)}</p><button class="secondary-button" data-action="review-reload">重新读取</button></section>`;}
}
function updateReviewSelection(){
 if(!$('review-selection-count'))return;
 $('review-selection-count').textContent=`已选 ${reviewSelected.size} 项`;
 const selected=judgmentItems().filter(r=>reviewSelected.has(r.key)),batch=document.querySelector('[data-action="review-batch"]');
 if(batch){batch.disabled=!selected.length||reviewBusy;batch.textContent=!selected.length?'批量处理':selected.some(r=>r.kind==='completeness')?'批量补充相同资料':'确认所选关系';}
 const eligible=reviewPageRows().filter(reviewRowSelectable),n=eligible.filter(r=>reviewSelected.has(r.key)).length;
 $('review-select-all').checked=eligible.length>0&&n===eligible.length;$('review-select-all').indeterminate=n>0&&n<eligible.length;
 $('review-select-all').disabled=!eligible.length||reviewBusy;
}
async function submitReviews(items){
 if(reviewBusy)return;reviewBusy=true;
 document.querySelectorAll('.review-actions button,.review-batchbar button').forEach(b=>b.disabled=true);
 try{await api('/api/people/reviews',{version:reviewQueue.version,items});for(const i of items){reviewSelected.delete(i.key);reviewDrafts.delete(i.key);}await load();toast(`已保存 ${items.length} 项处理结果。`);}
 catch(err){const out=$('review-error');if(out){out.hidden=false;out.textContent=err.message;out.scrollIntoView({block:'center'});}throw err;}
 finally{reviewBusy=false;document.querySelectorAll('.review-actions button,.review-batchbar button').forEach(b=>b.disabled=false);updateReviewSelection();}
}
async function reviewAction(button){
 const action=button.dataset.action;
 if(action==='review-reload')return load();
 if(action==='review-select'||action.startsWith('review-gap-')||action.startsWith('review-page-'))return gapReviewAction(button);
 if(action==='review-filter'){reviewFilter=button.dataset.filter;reviewPageIndex=0;reviewActiveKey='';reviewSelected.clear();drawReviews();document.querySelector(`[data-action="review-filter"][data-filter="${reviewFilter}"]`)?.focus();return;}
 if(reviewBusy)return;
 if(action==='review-batch'){
   if(reviewQueue.items.some(r=>reviewSelected.has(r.key)&&r.kind==='completeness'))return openGapBatch();
   const items=reviewQueue.items.filter(r=>reviewSelected.has(r.key)&&r.can_confirm&&r.state==='pending');
   if(!items.length)return;
   return submitReviews(items.map(r=>({key:r.key,decision:'confirm',note:reviewNote(r)})));
 }
 const r=reviewQueue.items.find(r=>r.key===button.dataset.key);if(!r)return;
 if(action==='review-edit-role'){
   $('detail-content').innerHTML=`<header class="dialog-header"><h2 id="detail-title">修正 ${escape(r.title)}</h2><button class="icon-button" data-action="close-detail" aria-label="关闭">×</button></header><form id="review-role-form" class="dialog-body" data-key="${escape(r.key)}"><label class="form-field">关联对象<select name="target_id">${data.records[r.target_kind].map(t=>`<option value="${escape(t.id)}" ${t.id===r.target_id?'selected':''}>${escape(t.name)}</option>`).join('')}</select></label><label class="form-field">角色<input name="label" maxlength="120" value="${escape(r.description)}" required></label><label class="form-field">分工说明<textarea name="detail" maxlength="2000" rows="4">${escape(r.detail)}</textarea></label><label class="form-field">我的备注<textarea name="note" maxlength="4000" rows="2">${escape(reviewNote(r))}</textarea></label>${relationDimensionFields(r)}<p id="review-role-error" class="form-error" role="alert" hidden></p><div class="form-footer"><button class="primary-button">保存修正并确认</button></div></form>`;
   $('detail-dialog').showModal();return;
 }
 const decision=action.replace('review-','');
 return submitReviews([{key:r.key,decision,note:reviewNote(r)}]);
}
async function saveReviewRole(event){
 event.preventDefault();if(reviewBusy)return;const f=event.target,values=Object.fromEntries(new FormData(f)),note=values.note;delete values.note;
 try{await submitReviews([{key:f.dataset.key,decision:'confirm',values,note}]);$('detail-dialog').close();}catch(err){$('review-role-error').hidden=false;$('review-role-error').textContent=err.message;}
}
document.addEventListener('input',event=>{
 if(event.target.dataset.reviewNote)reviewDrafts.set(event.target.dataset.reviewNote,event.target.value);
 if(event.target.id==='review-search'){reviewSearch=event.target.value;reviewPageIndex=0;reviewActiveKey='';reviewSelected.clear();const pos=event.target.selectionStart;drawReviews();$('review-search').focus();try{$('review-search').setSelectionRange(pos,pos);}catch{}}
});
document.addEventListener('change',event=>{
 const key=event.target.dataset.reviewSelect;
 if(key){if(event.target.checked)reviewSelected.add(key);else reviewSelected.delete(key);updateReviewSelection();}
 if(event.target.id==='review-select-all'){for(const r of reviewPageRows().filter(reviewRowSelectable)){if(event.target.checked)reviewSelected.add(r.key);else reviewSelected.delete(r.key);}drawReviews();}
});
