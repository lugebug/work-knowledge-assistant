'use strict';
// Independent of the application dependencies: a failed script must not hide the recovery path.
(() => {
  let ready=false, failure='', timer;
  function show(message) {
    if(ready)return;
    failure=message;
    const content=document.getElementById('content');if(!content)return;
    const box=document.createElement('section');box.className='panel empty';
    const title=document.createElement('h2');title.textContent='工作台没有加载完成';
    const explanation=document.createElement('p');explanation.textContent=message;
    const retry=document.createElement('button');retry.className='primary-button';retry.textContent='重新加载页面';retry.onclick=()=>location.reload();
    box.append(title,explanation,retry);content.replaceChildren(box);
    document.getElementById('data-status').textContent='加载失败，可重试';
    const refresh=document.getElementById('refresh');refresh.disabled=false;refresh.onclick=()=>location.reload();
  }
  window.workbenchBoot={ready(){ready=true;clearTimeout(timer);},failed:show};
  window.addEventListener('error',e=>{
    if(e.target?.tagName==='SCRIPT')show('页面脚本未能读取，请重新加载。若仍失败，请重新运行工作台启动脚本。');
    else if(e.error||e.message)show('页面初始化出现错误，请重新加载。若仍失败，请保留当前页面地址以便定位。');
  },true);
  window.addEventListener('unhandledrejection',()=>show('页面初始化未完成，请重新加载。'));
  document.addEventListener('DOMContentLoaded',()=>{
    if(location.protocol==='file:')return show('请通过工作台启动脚本打开本机网址，不能直接打开网页文件。');
    if(failure)show(failure);
  });
  timer=setTimeout(()=>show('读取已超过20秒，请重新加载；若服务已关闭，请重新运行工作台启动脚本。'),20000);
})();
