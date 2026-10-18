'use strict';
let backgroundRefreshing=false,recordLoading=false,liveChecking=false,liveInstance=null,livePending=false;
const viewFilterIds=['search','state-filter','category-filter','scope-filter','show-archived','priority-filter','people-org-filter','people-project-filter'];
function liveBlocked(){return pendingForm||judgmentSaving||workFocusSaving||document.querySelector('dialog[open]')||[...judgmentDrafts.entries()].some(([id,d])=>d.dirty&&!['resolved','dismissed'].includes(judgmentData?.items.find(c=>c.id===id)?.state));}
function captureView(){const focus=document.activeElement;return {filters:viewFilterIds.map(id=>[id,$(id).value,$(id).checked]),x:scrollX,y:scrollY,focus:focus?.id,start:focus?.selectionStart,end:focus?.selectionEnd,queue:document.querySelector('.judgment-queue')?.scrollTop,expanded:[...document.querySelectorAll('#content details[open]')].map(d=>d.querySelector('summary')?.textContent)};}
function restoreFilters(view){if(view)for(const [id,value,checked] of view.filters){$(id).value=value;$(id).checked=checked;}}
function restoreView(view){if(!view)return;for(const d of document.querySelectorAll('#content details'))if(view.expanded.includes(d.querySelector('summary')?.textContent))d.open=true;const el=view.focus&&$(view.focus);el?.focus({preventScroll:true});if(view.start!=null&&el?.setSelectionRange)el.setSelectionRange(view.start,view.end);const queue=document.querySelector('.judgment-queue');if(queue&&view.queue!=null)queue.scrollTop=view.queue;window.scrollTo(view.x,view.y);}
function liveStatus(){if(!data)return;$('data-status').textContent=livePending?'记录有更新，完成当前输入并关闭详情后自动同步':`读取于 ${formatTime(data.generated_at)} · 记录自动同步；来源按定期任务采集`;}
async function checkRecordUpdates(){
 if(liveChecking||recordLoading||!data||document.hidden)return;
 liveChecking=true;
 try{const current=await api('/api/version');if(liveInstance!==current.instance){token=(await api('/api/session')).token;liveInstance=current.instance;}if(current.change_token===data.change_token){livePending=false;liveStatus();return;}livePending=true;if(liveBlocked()){liveStatus();return;}backgroundRefreshing=true;await load(false,{background:true});}catch{if(data)$('data-status').textContent='自动同步暂未连通，稍后重试；当前内容保留';}finally{backgroundRefreshing=false;liveChecking=false;}
}
setInterval(checkRecordUpdates,10000);
window.addEventListener('focus',checkRecordUpdates);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)checkRecordUpdates();});
document.addEventListener('close',()=>setTimeout(checkRecordUpdates,0),true);
