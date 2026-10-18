'use strict';
let gapBatchRow=null,gapBatchTargets=[];
function reviewPageRows(){const rows=reviewRows();reviewPageIndex=Math.max(0,Math.min(reviewPageIndex,Math.ceil(rows.length/reviewPageSize)-1));return rows.slice(reviewPageIndex*reviewPageSize,(reviewPageIndex+1)*reviewPageSize);}
function reviewRowSelectable(r){return r.state==='pending'&&(r.kind==='completeness'||r.can_confirm);}
function drawReviewWorkspace(){
 const all=judgmentItems(),pending=all.filter(r=>r.state==='pending'),summary=reviewQueue.completeness||{},rows=reviewRows(),pageRows=reviewPageRows();
 if(!pageRows.some(r=>r.key===reviewActiveKey))reviewActiveKey=pageRows[0]?.key||'';
 const current=pageRows.find(r=>r.key===reviewActiveKey),selected=all.filter(r=>reviewSelected.has(r.key)),gaps=selected.some(r=>r.kind==='completeness');
 const count=k=>pending.filter(r=>reviewCategory(r)===k).length;
 const filters=[['pending','全部待处理',pending.length],['projects','项目',count('projects')],['people','人员',count('people')],['organizations','组织',count('organizations')],['departments','部门',count('departments')],['identity','身份与账号',count('identity')],['other','其他资料',count('other')],['deferred','暂不知道',pending.filter(r=>r.kind==='completeness'&&r.dimensions.some(d=>d.state==='deferred')).length],['done','已处理',all.filter(r=>r.state!=='pending').length]];
 $('content').innerHTML=`<div class="review-summary"><div><strong>${summary.incomplete_entities||0}</strong><span>份档案关键资料未齐</span></div><div><strong>${summary.missing_dimensions||0}</strong><span>项关键资料待补</span></div><p>我先根据资料补齐；仍缺的内容和关系疑点集中在这里。选一份档案，可一次补完多个缺项。<strong>暂不知道仍计入未解决；不适用需说明原因。</strong></p></div>
 <details class="review-rules"><summary>查看各类档案的关键维度</summary>${Object.entries(reviewQueue.critical_dimensions||{}).map(([k,ds])=>`<p><strong>${escape(gapKindNames[k])}：</strong>${ds.map(d=>escape(d.label)+(d.required==='internal'?'（内部同事）':'')).join('、')}</p>`).join('')}</details>
 <div class="review-filterbar"><div class="review-filters" aria-label="待判断分类">${filters.map(([key,name,n])=>`<button class="${reviewFilter===key?'primary':'secondary'}-button" data-action="review-filter" data-filter="${key}" aria-pressed="${reviewFilter===key}">${name} ${n}</button>`).join('')}</div><label class="search-box"><span aria-hidden="true">⌕</span><input id="review-search" type="search" aria-label="搜索档案、缺项或依据" placeholder="搜索姓名、项目、组织或缺项" value="${escape(reviewSearch)}"></label></div>
 <div class="review-batchbar"><label><input id="review-select-all" type="checkbox">选择本页</label><span id="review-selection-count" role="status">已选 ${reviewSelected.size} 项</span><button class="primary-button" data-action="review-batch" ${selected.length?'':'disabled'}>${gaps?'批量补充相同资料':'批量处理'}</button><button class="quiet-button" data-action="review-reload">重新读取，保留输入</button><small>可批量指定相同组织、部门、负责人或处理状态。</small></div>
 <div id="review-error" class="form-error" role="alert" hidden></div><div class="review-workspace"><section class="review-queue panel" aria-label="待处理档案"><div class="review-queue-heading"><strong>${rows.length} 份档案／关系</strong><span>第 ${rows.length?reviewPageIndex+1:0} / ${Math.ceil(rows.length/reviewPageSize)} 页</span></div><div class="review-queue-rows">${pageRows.map(r=>`<div class="review-queue-item ${r.key===reviewActiveKey?'active':''}">${reviewRowSelectable(r)?`<input type="checkbox" data-review-select="${escape(r.key)}" aria-label="选择 ${escape(r.title)}" ${reviewSelected.has(r.key)?'checked':''}>`:'<span></span>'}<button class="review-queue-button" data-action="review-select" data-key="${escape(r.key)}" aria-current="${r.key===reviewActiveKey}"><span class="review-type">${escape(r.kind==='completeness'?gapKindNames[r.target_kind]:'关系判断')}</span><strong>${escape(r.title)}</strong><small>${escape(r.kind==='completeness'?(r.missing_count?`缺 ${r.missing_count} 项：${r.description}`:'关键资料已处理'):r.description)}</small>${gapDrafts.has(r.key)?'<span class="draft-indicator">有未保存输入</span>':''}</button></div>`).join('')||'<p class="panel-body">没有符合当前筛选的事项。</p>'}</div><div class="review-pagination"><button class="secondary-button" data-action="review-page-prev" ${reviewPageIndex?'':'disabled'}>上一页</button><button class="secondary-button" data-action="review-page-next" ${(reviewPageIndex+1)*reviewPageSize<rows.length?'':'disabled'}>下一页</button></div></section><section class="review-editor-area" aria-label="填写与判断">${current?(current.kind==='completeness'?gapEditor(current):reviewCard(current,0)):`<div class="panel empty"><h2>${reviewSearch?'没有匹配的事项':'当前分类没有待处理项'}</h2><p>可切换分类或搜索条件；新出现的关键缺项会自动进入这里。</p></div>`}</section></div>`;
 updateReviewSelection();
}
function gapShowError(message){const out=(gapBatchRow?$('gap-batch-error'):$('gap-save-error'))||$('review-error');if(out){out.hidden=false;out.textContent=message;out.scrollIntoView({block:'nearest'});}}
function gapNextKey(){const rows=reviewRows(),i=rows.findIndex(r=>r.key===reviewActiveKey);return rows[i+1]?.key||rows[0]?.key||'';}
function gapSelectNext(){reviewActiveKey=gapNextKey();reviewPageIndex=Math.max(0,Math.floor(reviewRows().findIndex(r=>r.key===reviewActiveKey)/reviewPageSize));drawReviews();$('review-editor-title')?.focus();}
async function submitGapAnswers(items,nextKey=''){
 if(reviewBusy)return;reviewBusy=true;
 document.querySelectorAll('#gap-answer-form button,#gap-batch-form button,.review-batchbar button').forEach(b=>b.disabled=true);
 try{
  const result=await api('/api/reviews/complete',{version:reviewQueue.version,items});
  for(const item of items){gapDrafts.delete(item.key);reviewSelected.delete(item.key);}gapDrafts.delete('batch');
  if(nextKey)reviewActiveKey=nextKey;
  if(gapBatchRow){gapBatchRow=null;gapBatchTargets=[];$('detail-dialog').close();}
  await load();
  if(nextKey&&reviewRows().some(r=>r.key===nextKey))reviewActiveKey=nextKey;
  reviewPageIndex=Math.max(0,Math.floor(reviewRows().findIndex(r=>r.key===reviewActiveKey)/reviewPageSize));drawReviews();
  $('review-editor-title')?.focus();toast(`已保存 ${result.dimensions} 项答案／处理状态。`);
 }catch(err){gapShowError(err.message);throw err;}
 finally{reviewBusy=false;document.querySelectorAll('#gap-answer-form button,#gap-batch-form button,.review-batchbar button').forEach(b=>b.disabled=false);updateReviewSelection();}
}
async function saveGapForm(event){
 event.preventDefault();if(reviewBusy)return;
 const row=gapCurrentRow();try{const answers=gapSerialize(row);if(!Object.keys(answers).length)throw new Error('还没有填写新的答案；可先看下一条，或明确选择暂不知道。');await submitGapAnswers([{key:row.key,answers}],gapNextKey());}catch(err){gapShowError(err.message);}
}
function openGapBatch(){
 const rows=reviewQueue.items.filter(r=>reviewSelected.has(r.key));
 if(!rows.length||rows.some(r=>r.kind!=='completeness'))throw new Error('请只选择需要补资料的档案；关系确认可单独批量处理。');
 const first=rows[0],common=first.dimensions.filter(d=>d.state!=='filled'&&rows.every(r=>r.dimensions.some(x=>x.key===d.key&&x.editor===d.editor&&x.target===d.target&&x.state!=='filled')));
 if(!common.length)throw new Error('这些档案没有相同缺项。请先按人员、组织或项目筛选，再选择要一起处理的档案。');
 gapBatchTargets=rows;gapBatchRow={...first,key:'batch',title:'批量补充',dimensions:common};gapDrafts.delete('batch');
 $('detail-content').innerHTML=`<header class="dialog-header"><div><h2 id="detail-title">批量补充 ${rows.length} 份档案</h2><p>${escape(rows.slice(0,5).map(r=>r.title).join('、'))}${rows.length>5?' 等':''}</p></div><button class="icon-button" data-action="close-detail" aria-label="关闭">×</button></header><form id="gap-batch-form" class="dialog-body"><label class="form-field">统一补充哪一项<select id="gap-batch-dimension">${common.map(d=>`<option value="${d.key}">${escape(d.label)}</option>`).join('')}</select></label><div id="gap-batch-field">${gapField(gapBatchRow,common[0])}</div><p class="notice">下面的同一个答案／处理状态将应用到已选的 ${rows.length} 份档案；一次保存全部成功或全部不保存。</p><div id="gap-batch-error" class="form-error" role="alert" hidden></div><div class="form-footer"><button type="button" class="secondary-button" data-action="close-detail">取消</button><button class="primary-button" type="submit">保存到所选档案</button></div></form>`;
 $('detail-dialog').showModal();
}
async function gapReviewAction(button){
 const action=button.dataset.action;
 if(action==='review-select'){reviewActiveKey=button.dataset.key;drawReviews();$('review-editor-title')?.focus();return;}
 if(action==='review-page-prev'||action==='review-page-next'){reviewPageIndex+=action.endsWith('next')?1:-1;reviewActiveKey='';drawReviews();return;}
 if(action==='review-gap-next')return gapSelectNext();
 const row=gapCurrentRow();if(!row||row.kind!=='completeness')return;
 if(action==='review-gap-new'){newRecord(button.dataset.target);return;}
 if(action==='review-gap-defer-all'){
  for(const d of row.dimensions.filter(d=>['missing','deferred'].includes(d.state)&&!gapDraft(row,d)))gapPutDraft(row,d.key,{action:'defer',note:'暂不知道，后续补充。'});
  drawReviews();return;
 }
 const dim=row.dimensions.find(d=>d.key===button.dataset.dimension);if(!dim)return;
 const field=button.closest('[data-gap-dimension]'),draft=gapReadField(field,row,dim);
 if(action==='review-gap-suggest'){draft.value=button.dataset.value;draft.raw=gapOptionName(dim.target,draft.value,row);draft.action='set';}
 if(action==='review-gap-add'){
  const target=dim.editor==='organizations'?'organizations':dim.target,id=gapOptions(target,row).find(o=>o.text===draft.raw.trim())?.id;
  if(!id)throw new Error('请先输入并选择一个已有档案。');
  if(dim.editor==='organizations'){const role=draft.orgRole||'customer_ids';draft.value={...(draft.value||{}),[role]:[...new Set([...(draft.value?.[role]||[]),id])]};}
  else draft.value=[...new Set([...(Array.isArray(draft.value)?draft.value:[]),id])];
  draft.raw='';
 }
 if(action==='review-gap-remove'){
  if(dim.editor==='organizations'){draft.value={...(draft.value||{})};draft.value[button.dataset.role]=(draft.value[button.dataset.role]||[]).filter(id=>id!==button.dataset.value);if(!draft.value[button.dataset.role].length)delete draft.value[button.dataset.role];}
  else draft.value=(draft.value||[]).filter(id=>id!==button.dataset.value);
 }
 gapPutDraft(row,dim.key,draft);gapRefreshField(row,dim);
}
document.addEventListener('submit',event=>{
 if(event.target.id==='gap-answer-form')saveGapForm(event);
 if(event.target.id==='gap-batch-form'){
  event.preventDefault();if(reviewBusy)return;
  try{const key=$('gap-batch-dimension').value,answers=gapSerialize(gapBatchRow);if(!answers[key])throw new Error('请先填写批量答案或选择处理状态。');submitGapAnswers(gapBatchTargets.map(r=>({key:r.key,answers:{[key]:answers[key]}}))).catch(()=>{});}catch(err){gapShowError(err.message);}
 }
});
document.addEventListener('change',event=>{if(event.target.id==='gap-batch-dimension'){gapDrafts.delete('batch');$('gap-batch-field').innerHTML=gapField(gapBatchRow,gapBatchRow.dimensions.find(d=>d.key===event.target.value));}});
document.addEventListener('keydown',event=>{if(event.ctrlKey&&event.key==='Enter'&&event.target.closest('#gap-answer-form')){event.preventDefault();saveGapForm(event);}});
document.addEventListener('click',event=>{if(event.target.closest('[data-action="close-detail"]')){gapBatchRow=null;gapBatchTargets=[];gapDrafts.delete('batch');}});
document.addEventListener('cancel',event=>{if(event.target.id==='detail-dialog'){gapBatchRow=null;gapBatchTargets=[];gapDrafts.delete('batch');}},true);
