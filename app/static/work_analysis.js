'use strict';
let workProposalDraft=null;
function workView(r){
 if(typeof detail!=='undefined'&&dialogKind==='tasks'&&detail?.record.id===r.id&&detail.record.revision===r.revision&&detail.work_analysis)return detail.work_analysis;
 return data?.work_analysis?.[r.id]||{current:[r.progress,r.result].filter(Boolean).join(' ')||'当前情况尚未整理。',next_step:workClosed(r)?'工作已结束。':r.next_action||'下一步尚未明确。',next_mode:'recorded'};
}
function workSummary(r){
 const view=workView(r),mode={suggestion:'建议',arrangement:'已有安排',accepted:'已采纳'}[view.next_mode];
 return `<dl class="work-summary"><div><dt>当前情况</dt><dd>${escape(view.current)}</dd></div><div><dt>下一步（含时间）${mode?` <span class="tag ${view.next_mode==='suggestion'?'amber':'gray'}">${mode}</span>`:''}</dt><dd>${escape(view.next_step)}</dd>${view.stale?'<small class="work-review-note">情况有变化，下一步待复核。</small>':''}</div></dl>`;
}
function workNextActions(r){
 const view=workView(r);
 if(!view.can_decide)return '';
 const attrs=`data-id="${escape(r.id)}" data-revision="${r.revision}" data-context="${escape(view.context_token)}"`;
 return `<div class="work-next-actions"><button class="secondary-button" data-action="work-next-accept" ${attrs}>采纳下一步</button><button class="table-action" data-action="work-next-edit" ${attrs}>调整后采纳</button><button class="table-action" data-action="work-next-dismiss" ${attrs}>不采纳这条建议</button></div>`;
}
function workEvidenceReviews(r){
 if(typeof detail==='undefined'||detail?.record.id!==r.id)return '';
 const reviews=detail.work_evidence_reviews||[];
 const links=detail.external_task_links||[];
 const linked=links.length?`<section class="work-analysis-basis"><h3>关联的飞书任务</h3>${links.map(link=>`<p><a href="${escape(link.url)}" target="_blank" rel="noopener noreferrer">${escape(link.name)}</a> · ${link.relation==='same'?'同一事项':'其中一部分'}</p><p>飞书当前${link.status==='done'?'已完成':'未完成'}${link.due?.value?' · 原截止：'+escape(link.due.value):''}；本地工作按综合证据判断。</p><p>${escape(link.reason)}</p>`).join('')}</section>`:'';
 if(!reviews.length)return linked;
 const actions={advance:'推进工作',close:'关闭工作',decide:'工作待判断',unchanged:'无工作变化'};
 const outcomes={applied:'已保存',needs_decision:'当时已提交待你判断',protected_decision:'保留已有决定',unchanged:'已核对，无需更新'};
 return linked+`<section class="work-analysis-basis"><h3>工作推进与关闭记录</h3>${reviews.map(review=>`<details class="work-source-group"><summary>${escape(formatTime(review.reviewed_at))} · ${review.outcomes.map(item=>escape(actions[item.action]||'工作核对')).join('、')}</summary><p>${escape(review.summary)}</p>${review.outcomes.map(item=>`<p><strong>${escape(outcomes[item.outcome]||'已记录')}</strong>：${escape(item.reason)}</p>`).join('')}${evidenceSources(review.sources||[])}</details>`).join('')}</section>`;
}
function workBackground(r,full=false){
 const a=r._work_analysis,view=workView(r);
 const pairs=[['背景与要求',r.background],['前置条件',r.dependencies],['已有结果',r.result],['原文时间安排',r.schedule_note],['已登记日期',r.due_date],['补充说明',r.notes]];
 const meta=full?`<p class="muted">${escape(r.project_id?projectName(r.project_id):'独立工作')} · ${escape(r.owner_id?relationName('owner_id',r.owner_id):r.owner||'承接人未明确')}</p>${r.participant_ids?.length?`<p>参与：${escape(relationName('participant_ids',r.participant_ids))}</p>`:''}`:'';
 const analysis=a?`<section class="work-analysis-basis"><h3>本次分析</h3>${view.stale?`<p class="work-review-note">${escape(view.reason)}；上次分析保留供核对。</p>`:''}${view.alternative?`<p><strong>新的推进建议：</strong>${escape(view.alternative)}</p>`:''}<p>${escape(a.rationale)}</p><p>${escape(a.timing_basis)}</p><small class="muted">整理于 ${escape(formatTime(a.at))} · ${escape(({pending:'建议尚未采纳',accepted:'已采纳',dismissed:'未采纳'})[a.decision?.state]||'资料整理')}</small>${workNextActions(r)}${a.sources?.length?`<details class="work-source-group"><summary>分析依据 · ${a.sources.length} 条</summary>${evidenceSources(a.sources)}</details>`:''}</section>`:'';
 return `<details class="work-background"><summary>背景与依据</summary><div class="work-background-body">${meta}${pairs.filter(([,v])=>v).map(([name,value])=>`<section><h3>${name}</h3><p>${escape(value)}</p></section>`).join('')}${priorityExplanation(r,full)}${analysis}${full?followThroughDetailHTML()+workEvidenceReviews(r):''}${workEvidence(r)}${full?timeline(r)+relationshipsHTML():`<button class="table-action" data-action="detail" data-kind="tasks" data-id="${escape(r.id)}">查看完整处理过程</button>`}</div></details>`;
}
function workFormFields(){
 const main=new Set(['title','progress','next_action']);
 return specs.tasks.filter(s=>main.has(s[0])).map(fieldHTML).join('')+`<details class="project-form-group"><summary>状态、人员、背景及其他资料</summary><div class="form-grid">${specs.tasks.filter(s=>!main.has(s[0])).map(fieldHTML).join('')}</div></details>`;
}
function workProposalRows(){
 if(!['mine','all'].includes(selectedWorkScope()))return '';
 const query=$('search').value.trim().toLocaleLowerCase();
 const proposals=(data.work_proposals||[]).filter(p=>p.state==='pending'&&(!query||(p.title+' '+p.current+' '+p.next_step+' '+projectName(p.project_id)).toLocaleLowerCase().includes(query)));
 if(!proposals.length)return '';
 return `<section class="panel work-proposals"><header class="panel-header"><h2>建议的新工作</h2><span class="muted">${proposals.length} 项 · 尚未成为明确安排</span></header>${proposals.map(p=>`<article class="work-row compact"><h3>${escape(p.title)}</h3><dl class="work-summary"><div><dt>当前情况</dt><dd>${escape(p.current)}</dd></div><div><dt>下一步（含时间） <span class="tag amber">建议</span></dt><dd>${escape(p.next_step)}</dd></div></dl><details class="work-background"><summary>背景与依据</summary><div class="work-background-body"><p>${escape(p.rationale)}</p>${evidenceSources(p.sources||[])}</div></details><button class="table-action" data-action="work-proposal-open" data-id="${escape(p.id)}">采纳、调整或归入已有工作</button></article>`).join('')}</section>`;
}
async function handleWorkAnalysisAction(button){
 const a=button.dataset.action;
 if(a==='work-proposal-open')return showWorkProposal(button.dataset.id);
 if(a==='work-proposal-cancel'){workProposalDraft=null;$('detail-dialog').close();return;}
 if(a==='work-next-edit'){
  await openDetail('tasks',button.dataset.id);
  if(detail?.record.revision!==Number(button.dataset.revision)){toast('工作已有更新，请先查看当前内容。');return;}
  return showWorkNextEdit();
 }
 if(pendingForm)return;
 pendingForm=true;button.disabled=true;
 try{
  const rid=button.dataset.id;
  await api('/api/work/next-decision',{id:rid,revision:Number(button.dataset.revision),answer:{action:a==='work-next-accept'?'accept':'dismiss',context_token:button.dataset.context}});
  await load();if($('detail-dialog').open&&dialogKind==='tasks'&&detail?.record.id===rid)await openDetail('tasks',rid);
  toast(a==='work-next-accept'?'下一步已采纳。':'已记录不采纳，同一建议不会自动重开。');
 }catch(error){toast(error.message);}finally{pendingForm=false;button.disabled=false;}
}
function showWorkNextEdit(){
 const r=detail.record;editing=false;
 $('detail-content').innerHTML=headerHTML()+`<form id="work-next-form" class="dialog-body" data-id="${escape(r.id)}" data-revision="${r.revision}" data-context="${escape(detail.work_analysis.context_token)}"><label class="form-field">下一步（含时间）<textarea name="next_step" required maxlength="12000" rows="6">${escape(r._work_analysis.next_step)}</textarea></label><label class="form-field">调整说明（选填）<input name="note" maxlength="2000"></label><div id="work-next-error" class="form-error" role="alert" hidden></div><div class="form-footer"><button type="button" class="secondary-button" data-action="cancel-edit">返回工作</button><button class="primary-button" type="submit">保存并采纳</button></div></form>`;
 requestAnimationFrame(()=>document.querySelector('#work-next-form textarea')?.focus());
}
async function saveWorkNext(event){
 event.preventDefault();if(pendingForm)return;pendingForm=true;
 const form=event.target,button=form.querySelector('[type=submit]'),values=Object.fromEntries(new FormData(form));button.disabled=true;
 try{const rid=form.dataset.id;await api('/api/work/next-decision',{id:rid,revision:Number(form.dataset.revision),answer:{action:'edit',...values,context_token:form.dataset.context}});await load();await openDetail('tasks',rid);toast('已按你的修改更新下一步。');}
 catch(error){$('work-next-error').hidden=false;$('work-next-error').textContent=error.message;}
 finally{pendingForm=false;button.disabled=false;}
}
function showWorkProposal(id){
 const p=(data.work_proposals||[]).find(r=>r.id===id);if(!p||p.state!=='pending'){toast('该建议已处理，请刷新后查看。');return;}
 workProposalDraft={...p};editing=false;dialogKind='work_proposals';
 const targets=data.records.tasks.filter(r=>!r.archived&&!workClosed(r));
 $('detail-content').innerHTML=`<header class="dialog-header"><div><p class="eyebrow">工作建议</p><h2 id="detail-title">${escape(p.title)}</h2></div><button class="icon-button" data-action="work-proposal-cancel" aria-label="关闭工作建议">×</button></header><form id="work-proposal-form" class="dialog-body"><p>${escape(p.current)}</p><label class="form-field">工作名称<input name="title" required maxlength="500" value="${escape(p.title)}"></label><label class="form-field">下一步（含时间）<textarea name="next_step" required maxlength="12000" rows="5">${escape(p.next_step)}</textarea></label><label class="form-field">如何处理<select name="action"><option value="accept">采纳为我的工作</option><option value="link">归入已有工作</option><option value="dismiss">不采纳</option></select></label><label class="form-field">归入哪项工作（选择归入时填写）<select name="target_id"><option value="">请选择</option>${targets.map(r=>`<option value="${escape(r.id)}">${escape(r.title)}${r.project_id?' · '+escape(projectName(r.project_id)):''}</option>`).join('')}</select></label><label class="form-field">说明（选填）<input name="note" maxlength="2000"></label><details class="work-background"><summary>背景与依据</summary><div class="work-background-body"><p>${escape(p.rationale)}</p>${evidenceSources(p.sources||[])}</div></details><div id="work-proposal-error" class="form-error" role="alert" hidden></div><div class="form-footer"><button type="button" class="secondary-button" data-action="work-proposal-cancel">暂不处理</button><button class="primary-button" type="submit">保存选择</button></div></form>`;
 if(!$('detail-dialog').open)$('detail-dialog').showModal();requestAnimationFrame(()=>document.querySelector('#work-proposal-form input')?.focus());
}
async function saveWorkProposal(event){
 event.preventDefault();if(pendingForm)return;pendingForm=true;
 const form=event.target,button=form.querySelector('[type=submit]'),values=Object.fromEntries(new FormData(form)),p=workProposalDraft;button.disabled=true;
 try{
  const answer={action:values.action,note:values.note};
  if(values.action==='accept'){answer.action=values.title!==p.title||values.next_step!==p.next_step?'edit':'accept';answer.title=values.title;answer.next_step=values.next_step;}
  if(values.action==='link'){const target=findRecord('tasks',values.target_id);if(!target)throw new Error('请选择要归入的现有工作。');Object.assign(answer,{target_id:target.id,target_revision:target.revision,next_step:values.next_step});}
  const result=await api('/api/work/proposal-decision',{id:p.id,revision:p.revision,answer});workProposalDraft=null;$('detail-dialog').close();await load();if(result.work)await openDetail('tasks',result.work.id);toast(answer.action==='dismiss'?'已记录不采纳。':'已保存到工作中。');
 }catch(error){$('work-proposal-error').hidden=false;$('work-proposal-error').textContent=error.message;}
 finally{pendingForm=false;button.disabled=false;}
}
