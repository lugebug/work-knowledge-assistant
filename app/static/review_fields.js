'use strict';
// Editors share record choices, while preserving drafts across filters and reloads.
let gapDrafts=new Map(),reviewActiveKey='',reviewPageIndex=0;
const reviewPageSize=20;
const gapKindNames={projects:'项目',people:'人员',contacts:'聊天账号',organizations:'组织',departments:'部门',capabilities:'产品能力',roadmap:'产品路线',demands:'需求',tasks:'待办',updates:'工作进展'};
const gapOrgRoles={customer_ids:'客户',channel_ids:'渠道',partner_ids:'合作伙伴',supplier_ids:'供应方',implementation_org_ids:'实施交付方',operating_org_ids:'运营方',internal_org_ids:'内部参与组织'};
function gapOptions(target,row){
 const rows=(data.records[target]||[]).filter(r=>!r.archived&&!(row?.target_kind===target&&row.id===r.id));
 const names=rows.map(titleOf),seen=new Map();
 return rows.map((r,i)=>{const name=names[i],n=(seen.get(name)||0)+1;seen.set(name,n);return {id:r.id,text:name+(names.filter(x=>x===name).length>1?`（同名档案${n}）`:'')};});
}
function gapOptionName(target,id,row){return gapOptions(target,row).find(o=>o.id===id)?.text||'';}
function gapDraft(row,dim){return gapDrafts.get(row.key)?.[dim.key]||null;}
function gapInputId(row,dim){return 'gap-'+encodeURIComponent(row.key+'-'+dim.key).replaceAll('%','_');}
function gapRelationInput(row,dim,target,value='',suffix=''){
 const id=gapInputId(row,dim)+suffix;
 return `<div class="gap-relation-input"><input id="${id}" data-gap-input="${escape(dim.key)}" data-gap-target="${target}" list="${id}-choices" autocomplete="off" aria-label="${escape(dim.label)}" placeholder="输入名称搜索已有${escape(gapKindNames[target]||'档案')}" value="${escape(value)}"><datalist id="${id}-choices">${gapOptions(target,row).map(o=>`<option value="${escape(o.text)}"></option>`).join('')}</datalist></div>`;
}
function gapValueEditor(row,dim,draft){
 const value=draft?.value??'',raw=draft?.raw??(dim.target?gapOptionName(dim.target,value,row):String(value||''));
 if(dim.editor==='relation')return gapRelationInput(row,dim,dim.target,raw)+(row.key==='batch'?'<small>若没有对应档案，请取消批量操作后先新建。</small>':`<button type="button" class="table-action" data-action="review-gap-new" data-target="${dim.target}">找不到？新建${gapKindNames[dim.target]}</button>`);
 if(dim.editor==='relations'){
  const selected=Array.isArray(value)?value:[];
  return `<div class="gap-add-line">${gapRelationInput(row,dim,dim.target,draft?.raw||'')}<button type="button" class="secondary-button" data-action="review-gap-add" data-dimension="${escape(dim.key)}">添加</button></div><div class="gap-chips">${selected.map(id=>`<span>${escape(gapOptionName(dim.target,id,row))}<button type="button" data-action="review-gap-remove" data-dimension="${escape(dim.key)}" data-value="${escape(id)}" aria-label="移除 ${escape(gapOptionName(dim.target,id,row))}">×</button></span>`).join('')}</div>`;
 }
 if(dim.editor==='organizations'){
  const selected=typeof value==='object'&&!Array.isArray(value)&&value?value:{};
  return `<div class="gap-add-line"><select data-gap-org-role aria-label="组织在项目中的角色">${Object.entries(gapOrgRoles).map(([k,n])=>`<option value="${k}" ${draft?.orgRole===k?'selected':''}>${n}</option>`).join('')}</select>${gapRelationInput(row,dim,'organizations',draft?.raw||'')}<button type="button" class="secondary-button" data-action="review-gap-add" data-dimension="${escape(dim.key)}">添加</button></div><div class="gap-chips">${Object.entries(selected).flatMap(([role,ids])=>ids.map(id=>`<span>${escape(gapOrgRoles[role])}：${escape(gapOptionName('organizations',id,row))}<button type="button" data-action="review-gap-remove" data-dimension="${escape(dim.key)}" data-role="${escape(role)}" data-value="${escape(id)}" aria-label="移除 ${escape(gapOptionName('organizations',id,row))}">×</button></span>`)).join('')}</div>`;
 }
 if(dim.editor==='choice')return `<select data-gap-input="${escape(dim.key)}" aria-label="${escape(dim.label)}"><option value="">请选择</option>${dim.options.map(o=>`<option ${value===o?'selected':''}>${escape(o)}</option>`).join('')}</select>`;
 if(dim.editor==='choices')return `<div class="gap-checks">${dim.options.map(o=>`<label><input type="checkbox" data-gap-choice="${escape(dim.key)}" value="${escape(o)}" ${Array.isArray(value)&&value.includes(o)?'checked':''}>${escape(o)}</label>`).join('')}</div>`;
 if(dim.editor==='date')return `<input type="date" data-gap-input="${escape(dim.key)}" aria-label="${escape(dim.label)}" value="${escape(value)}">`;
 return `<textarea rows="2" data-gap-input="${escape(dim.key)}" aria-label="${escape(dim.label)}" maxlength="12000" placeholder="填写实际情况">${escape(value)}</textarea>`;
}
function gapField(row,dim){
 const draft=gapDraft(row,dim),mode=draft?.action||(dim.state==='deferred'?'defer':dim.state==='not_applicable'?'not_applicable':'set');
 return `<fieldset class="gap-field" data-gap-dimension="${escape(dim.key)}"><legend>${escape(dim.label)} ${dim.state==='deferred'?'<span class="tag amber">暂不知道，仍未解决</span>':dim.state==='not_applicable'?'<span class="tag gray">已标记不适用</span>':''}</legend>${dim.hint?`<p class="form-hint">${escape(dim.hint)}</p>`:''}<label class="gap-mode">处理方式<select data-gap-mode="${escape(dim.key)}" aria-label="${escape(dim.label)}的处理方式"><option value="set" ${mode==='set'?'selected':''}>填写答案</option><option value="defer" ${mode==='defer'?'selected':''}>暂不知道</option><option value="not_applicable" ${mode==='not_applicable'?'selected':''}>不适用</option></select></label><div class="gap-value" ${mode!=='set'?'hidden':''}>${gapValueEditor(row,dim,draft)}${dim.suggestions?.length?`<div class="gap-suggestions"><span>已有相关人员，供你选择：</span>${dim.suggestions.map(s=>`<button type="button" class="quiet-button" data-action="review-gap-suggest" data-dimension="${escape(dim.key)}" data-value="${escape(s.id)}" title="${escape(s.reason)}">${escape(s.name)}</button>`).join('')}<small>这里列出相关人员，并未认定他们是负责人。</small></div>`:''}</div><label class="gap-note-label">${mode==='not_applicable'?'不适用的原因（必填）':'补充说明（选填）'}<textarea data-gap-note="${escape(dim.key)}" rows="1" maxlength="2000" placeholder="例如：日常工作不对应具体项目">${escape(draft?.note??dim.note??'')}</textarea></label></fieldset>`;
}
function gapEditor(row){
 const missing=row.dimensions.filter(d=>d.state!=='filled');
 return `<article class="gap-editor panel"><header class="panel-header"><div><span class="review-type">${escape(gapKindNames[row.target_kind])}</span><h2 id="review-editor-title" tabindex="-1">${escape(row.title)}</h2><p>关键维度 ${row.complete_count} / ${row.total_count} 已有答案或已明确不适用 · ${row.missing_count} 项仍缺</p></div><button class="table-action" data-action="detail" data-kind="${row.target_kind}" data-id="${escape(row.id)}">完整档案</button></header><div class="gap-context">${row.detail?`<p>${escape(row.detail)}</p>`:''}<details><summary>已有资料与核对依据</summary><p>${escape(row.basis)}</p>${personRefs(row.sources)||'<p>当前档案没有足以补齐下方内容的可展开依据。</p>'}</details></div><form id="gap-answer-form" data-key="${escape(row.key)}"><div class="gap-fields">${missing.map(d=>gapField(row,d)).join('')||'<p class="notice">本次缺项已处理。可打开完整档案修改已填答案。</p>'}</div><div id="gap-save-error" class="form-error" role="alert" hidden></div><div class="gap-savebar"><button type="button" class="quiet-button" data-action="review-gap-next">先看下一条</button><button type="button" class="secondary-button" data-action="review-gap-defer-all">剩余都暂不知道</button><button type="submit" class="primary-button">保存并下一条</button><small>只保存本次填写或明确选择的内容 · Ctrl + Enter</small></div></form></article>`;
}
function gapCurrentRow(){return gapBatchRow||reviewQueue?.items.find(r=>r.key===reviewActiveKey);}
function gapPutDraft(row,key,changes){
 const all=gapDrafts.get(row.key)||{},dim=row.dimensions.find(d=>d.key===key);
 all[key]={action:dim?.state==='deferred'?'defer':dim?.state==='not_applicable'?'not_applicable':'set',note:dim?.note||'',...(all[key]||{}),...changes};gapDrafts.set(row.key,all);
}
function gapReadField(field,row,dim){
 const mode=field.querySelector('[data-gap-mode]')?.value||'set',note=field.querySelector('[data-gap-note]')?.value||'';
 const input=field.querySelector('[data-gap-input]');
 const old=gapDraft(row,dim)||{};
 let value=old.value;
 if(dim.editor==='choices')value=Array.from(field.querySelectorAll('[data-gap-choice]:checked'),e=>e.value);
 else if(dim.editor==='relation')value=gapOptions(dim.target,row).find(o=>o.text===input?.value.trim())?.id||'';
 else if(!['relations','organizations'].includes(dim.editor))value=input?.value||'';
 return {action:mode,note,value,raw:input?.value||'',orgRole:field.querySelector('[data-gap-org-role]')?.value};
}
function gapRefreshField(row,dim){
 const field=(gapBatchRow?$('gap-batch-form'):$('gap-answer-form'))?.querySelector(`[data-gap-dimension="${dim.key}"]`);if(!field)return;
 field.outerHTML=gapField(row,dim);
}
function gapSerialize(row){
 const drafts=gapDrafts.get(row.key)||{},answers={};
 for(const [key,d] of Object.entries(drafts)){
  const spec=row.dimensions.find(s=>s.key===key);if(!spec)continue;
  let value=d.value;
  if(d.action==='set'&&spec.editor==='relation'){
   value=gapOptions(spec.target,row).find(o=>o.text===(d.raw||gapOptionName(spec.target,value,row)).trim())?.id;
   if(!value)throw new Error(`请为“${spec.label}”选择已有档案；找不到时可先新建。`);
  }
  if(d.action==='set'&&spec.editor==='relations'&&d.raw?.trim()){
   const id=gapOptions(spec.target,row).find(o=>o.text===d.raw.trim())?.id;
   if(!id)throw new Error(`请为“${spec.label}”选择已有档案。`);
   value=[...new Set([...(Array.isArray(value)?value:[]),id])];
  }
  if(d.action==='set'&&spec.editor==='organizations'&&d.raw?.trim()){
   const id=gapOptions('organizations',row).find(o=>o.text===d.raw.trim())?.id;
   if(!id)throw new Error('请选择已有组织。');
   const role=d.orgRole||'customer_ids';value={...(value||{}),[role]:[...new Set([...(value?.[role]||[]),id])]};
  }
  if(d.action==='set'&&(value===undefined||value===''||Array.isArray(value)&&!value.length))throw new Error(`“${spec.label}”尚未填写；也可选择暂不知道或不适用。`);
  answers[key]={action:d.action,note:d.note||'',...(d.action==='set'?{value}: {})};
 }
 return answers;
}
document.addEventListener('input',event=>{
 const field=event.target.closest('[data-gap-dimension]'),row=gapCurrentRow();
 if(field&&row?.kind==='completeness'){
  const dim=row.dimensions.find(d=>d.key===field.dataset.gapDimension);gapPutDraft(row,dim.key,gapReadField(field,row,dim));
 }
});
document.addEventListener('change',event=>{
 const field=event.target.closest('[data-gap-dimension]'),row=gapCurrentRow();
 if(field&&row?.kind==='completeness'){
  const dim=row.dimensions.find(d=>d.key===field.dataset.gapDimension);gapPutDraft(row,dim.key,gapReadField(field,row,dim));
  if(event.target.dataset.gapMode)gapRefreshField(row,dim);
 }
});
