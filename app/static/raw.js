/* Source collection UI: no project mutation, no raw writes from the browser. */
let rawData=null, rawPoll=null;
function sourceHealthHTML(health){
 if(!health?.available)return '';
 const names={wechat:'微信',feishu:'飞书'};
 return `<div class="source-health">${health.platforms.map(p=>`<p class="${p.failed||p.local_missing||p.never_read?'notice':'muted'}"><strong>${escape(names[p.platform]||p.platform)}</strong> · ${p.sources} 个来源${p.failed?` · ${p.failed} 个最近读取失败`:''}${p.running?` · ${p.running} 个正在更新`:''}${p.local_missing?` · ${p.local_missing} 个本机缺少历史`:''}${p.never_read_other?` · ${p.never_read_other} 个尚未成功读取`:''}<br>成功覆盖时间：${p.oldest_coverage?escape(formatTime(p.oldest_coverage)):'尚无'}${p.newest_coverage&&p.newest_coverage!==p.oldest_coverage?' 至 '+escape(formatTime(p.newest_coverage)):''}</p>`).join('')}${health.attention?'<p class="notice">部分来源尚未更新成功，最新进展可能还没入库。待整理为0也不代表这些资料已补齐。</p>':''}</div>`;
}
const rawStates={approved:'已批准',candidate:'待你判断',paused:'已暂停',rejected:'不采集'};
const runStates={running:'更新中',succeeded:'成功',failed:'失败，可重试',interrupted:'中断，可重试'};
function rawWhen(value){return value?formatTime(value):'尚未成功更新';}
async function renderRaw(){
  clearTimeout(rawPoll);
  try{
    const [current,changes]=await Promise.all([api('/api/raw/status'),api('/api/raw/changes')]);
    if(page!=='sources')return;
    rawData=current;
    const candidates=current.sources.filter(s=>s.state==='candidate');
    const count=state=>current.sources.filter(s=>s.state===state).length;
    const rows=[...current.sources].sort((a,b)=>(data.source_relevance?.[a.id]??3)-(data.source_relevance?.[b.id]??3)).map(s=>{
      const run=s.latest_run,busy=run?.state==='running',retry=run&&['failed','interrupted'].includes(run.state),manual=s.config.manual_only===true;
      const missingLocal=run?.result?.receipt?.local_table_found===false;
      const topics=s.config.topics?.join('、');
      const actions=manual?'<span class="muted">用户提供文字；通过Codex补充新版本</span>':s.state==='approved'?`<button class="secondary-button" data-action="raw-update" data-id="${escape(s.id)}" ${busy?'disabled':''}>${busy?'更新中…':retry?'重试更新':'更新原始资料'}</button><button class="quiet-button" data-action="raw-state" data-id="${escape(s.id)}" data-state="paused">暂停</button>`:
        `<button class="secondary-button" data-action="raw-state" data-id="${escape(s.id)}" data-state="approved">${s.state==='candidate'?'批准采集':'恢复采集'}</button>${s.state==='candidate'?`<button class="quiet-button" data-action="raw-state" data-id="${escape(s.id)}" data-state="rejected">不采集</button>`:''}`;
      return `<tr><td><strong>${escape(s.name)}</strong><span class="subline">${({wechat:'微信',feishu:'飞书',user:'用户提供'}[s.platform]||'其他来源')} · ${s.kind==='document'?'文档':s.kind==='task'?'任务':'工作会话'} · ${rawStates[s.state]}</span><span class="subline">${manual?'仅保存用户明确提供的文字':topics?'只读主题：'+escape(topics):'仅此已登记来源'}</span><details class="raw-basis"><summary>工作依据与范围</summary><p>${escape(s.basis.note||'已有工作来源')}</p>${(s.basis.sources||[]).map(r=>`<blockquote>${escape(r.quote)}</blockquote>`).join('')}<p>${manual?'首次入库':'首次回溯'}：${escape(formatTime(s.config.since))}</p></details></td>
      <td>${manual?'本次提供的文字':escape(rawWhen(s.sync?.covered_until))}<span class="subline">最近${manual?'保存':'成功'}：${escape(rawWhen(s.sync?.success_at))}</span><span class="subline">${s.objects} 条已归档</span></td>
      <td>${run?badge(missingLocal?'本机缺少历史':runStates[run.state]):'<span class="muted">未执行</span>'}${run?`<span class="subline">尝试于 ${formatTime(run.started_at)}</span>`:''}${run?.state==='succeeded'?`<span class="subline">新归档 ${run.result.new||0} · 版本变化 ${run.result.changed||0} · 已存在 ${run.result.unchanged||0}</span>`:''}${missingLocal?'<p class="notice">未找到本机会话记录，不能认定平台没有历史；覆盖时间未推进。</p>':''}${run?.error?`<p class="raw-error">${escape(run.error)}</p>`:''}</td><td><div class="raw-actions">${actions}</div></td></tr>`;
    }).join('');
    const changeRows=[...changes].sort((a,b)=>(data.source_relevance?.[a.source_id]??3)-(data.source_relevance?.[b.source_id]??3)).slice(0,30).map(c=>`<tr><td>${escape(c.name)}</td><td>${c.change_type==='new'?'新归档':'版本变化'}</td><td>${c.reviewed_at?'已整理':'待Codex整理'}</td><td><button class="table-action" data-action="raw-version" data-id="${escape(c.version_id)}">查看原文版本</button></td></tr>`).join('');
    $('content').innerHTML=`<div class="support-summary"><div><strong>${count('approved')}</strong><span>已批准来源</span></div><div><strong>${candidates.length}</strong><span>名单待判断</span></div><div><strong>${current.pending_changes}</strong><span>待整理变化</span></div></div>
      <p class="support-intro">微信只读取已登记工作来源；飞书全部可见会话按工作资料登记，不以成员所属企业筛选。聊天保存文字和微信已有语音转写，图片、文件、视频与音频不另存。原始资料更新与项目整理分别完成，采集不会覆盖你维护的内容。</p>
      ${sourceHealthHTML(current.health)}
      ${current.tasks_discovery?.errors?.length?`<p class="notice">飞书任务发现仍有 ${current.tasks_discovery.errors.length} 项读取缺口；已取得的任务继续整理，不能据此认定全部清单已覆盖。</p>`:''}
      ${candidates.length?'<p class="notice">发现有工作线索但身份或用途不够明确的候选。批准前不会读取其会话正文。</p>':''}
      ${panel('工作来源名单',`<div class="table-wrap raw-table"><table><thead><tr><th scope="col">来源与读取范围</th><th scope="col">成功覆盖至</th><th scope="col">最近一次更新</th><th scope="col">操作</th></tr></thead><tbody>${rows}</tbody></table></div>`)}
      <p class="notice">“新归档”表示首次进入这层原始资料，可能是已有历史消息；不等于新的项目进展。文档按本次修订保存；飞书任务保存当前文字、状态、成员和起止时间，旧任务仍未完成时保留当前快照。${escape(current.schedule?.description||'可手动更新来源；定期任务状态待刷新。')}</p>
      ${panel('待整理的原始变化',changeRows?`<div class="table-wrap"><table><thead><tr><th>来源</th><th>变化</th><th>项目整理</th><th>原文</th></tr></thead><tbody>${changeRows}</tbody></table></div>`:empty('尚无变化','更新后有新内容，会在这里列出；不会自动改写项目。'))}
      <p class="notice">此处展示最近30条；可通过Codex查询更多。原文只读，采集批次与历史版本保存在本机。</p>`;
    if(current.sources.some(s=>s.latest_run?.state==='running'))rawPoll=setTimeout(()=>{if(page==='sources')renderRaw();},2500);
  }catch(error){if(page==='sources')$('content').innerHTML=panel('来源读取失败',`<div class="panel-body"><p>${escape(error.message)}</p><button class="secondary-button" data-action="raw-reload">重试读取状态</button></div>`);}
}
async function rawAction(button){
  const action=button.dataset.action,s=rawData?.sources.find(s=>s.id===button.dataset.id);
  if(action==='raw-reload')return renderRaw();
  if(action==='raw-update'){
    if(!s)return;button.disabled=true;
    const previous=s.latest_run,retry=previous&&['failed','interrupted'].includes(previous.state)?previous.id:null;
    await api('/api/raw/update',{source_id:s.id,retry_of:retry});toast('已开始更新原始资料；项目维护内容保持不变');return renderRaw();
  }
  if(action==='raw-state'){
    if(!s)return;
    await api('/api/raw/source-state',{source_id:s.id,state:button.dataset.state,revision:s.revision});
    toast(button.dataset.state==='approved'?'已加入采集名单，可手动更新':'已停止该来源后续采集');return renderRaw();
  }
  if(action==='raw-version'){
    const r=await api('/api/raw/version?id='+encodeURIComponent(button.dataset.id));
    const supplied=r.payload.content_mode==='user_supplied_text';
    $('source-title').textContent=r.payload.title+' · '+(supplied?'用户提供的原文':r.payload.revision?'修订 '+r.payload.revision:'原始消息');
    $('source-body').textContent=(supplied?'入库时间：':'采集时间：')+formatTime(r.observed_at)+'\n来源时间：'+(r.payload.event_at||(supplied?'未确认':'文档版本'))+(supplied?'\n内容类型：用户提供文字，未读取或验证远端页面':r.payload.content_mode==='wechat_cached_voice_transcript'?'\n内容类型：微信已有语音转写（未逐字听音校对）':'')+'\n\n'+r.payload.body;
    $('source-dialog').showModal();
  }
}
