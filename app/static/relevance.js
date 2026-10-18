'use strict';
function relevanceRank(kind,id){return data?.relevance?.[kind]?.[id]?.rank??3;}
function compareRelevant(kind,a,b){return relevanceRank(kind,a.id)-relevanceRank(kind,b.id);}
function compareLinked(a,b){return relevanceRank(a.kind,a.record_id||a.id)-relevanceRank(b.kind,b.record_id||b.id);}
function recordQuestions(kind,id){const rows=(data.judgment_items||[]).filter(c=>c.targets.some(t=>t.kind===kind&&t.id===id));return rows.map(c=>`<a class="subline" href="#reviews/${encodeURIComponent(c.id)}">待你判断：${escape(c.question)}</a>`).join('');}
function followThroughNotice(){const c=data.follow_through;return c?.enabled&&c.pending?`<p class="notice">还有 ${c.pending_projects} 个项目、${c.pending_work} 项工作的后续承接或关键未知待Codex核对。无需你补填表单；确实需要你决定时，会列入“待你判断”。</p>`:'';}
function followThroughDetailHTML(){
 const review=detail?.follow_through;if(!review||!Object.keys(review.facets||{}).length)return '';
 if(review.stale)return '<p class="muted">工作情况有变化，补充判断待复核。</p>';
 return `<details class="support-section"><summary>有助推进的补充信息</summary>${Object.entries(review.facets).map(([key,facet])=>`<section><h3>${escape(review.facet_labels[key])} ${badge(facet.mode==='fact'?'有依据':'分析')}</h3><p>${escape(facet.text)}</p>${facet.sources?.length?`<details><summary>查看依据</summary>${evidenceSources(facet.sources)}</details>`:''}</section>`).join('')}</details>`;
}
function reviewInventoryHTML(){
 const inventory=data.review_inventory;if(!inventory)return '';
 const uncovered=(inventory.essential||[]).filter(r=>r.uncovered);
 const observations=(inventory.observations||[]).filter(r=>r.count);
 const questions=(inventory.essential||[]).filter(r=>r.questions?.length).length,protectedCount=inventory.summary?.protected_fields||0;
 return `${followThroughNotice()}${questions?`<p class="muted">常规检查识别的 ${questions} 处分工缺口已有对应问题；更多业务问题见上方。列入“待你判断”不等于已经解决。</p>`:''}${protectedCount?`<p class="muted">另有 ${protectedCount} 项沿用你之前的处理，不重复提问。</p>`:''}${uncovered.length?`<section class="panel panel-body"><h2>分工待Codex继续核对</h2><p>还有 ${uncovered.length} 项未形成具体问题；会先检查资料，确实需要你决定时再提出。</p>${uncovered.map(r=>`<p>${link(r.kind,{id:r.id,name:r.name})} · ${escape(r.label)}</p>`).join('')}</section>`:''}<details class="review-inventory"><summary>其他尚未记录的资料 · ${observations.length} 类</summary><p>这些空白不直接等同于待办。能从来源核实的继续补齐；影响推进且仍不能确定的，再形成上方具体问题。</p>${observations.map(group=>`<details><summary>${escape(group.label)} · ${group.count} 项</summary><p class="muted">${escape(group.explanation)}</p><div class="inventory-records">${[...group.items].sort((a,b)=>compareRelevant(group.kind,a,b)).map(r=>`<span>${link(group.kind,r)}</span>`).join('')}</div></details>`).join('')}</details>`;
}
