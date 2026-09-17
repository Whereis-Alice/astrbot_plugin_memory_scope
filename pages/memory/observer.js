/* Independent observer UI; shared by the authenticated plugin bridge and daemon. */
(function () {
  "use strict";
  const root = document.getElementById("observer-root");
  if (!root) return;
  const bridge = window.AstrBotPluginPage;
  const embedded = root.hasAttribute("data-embedded");
  let token = "", busy = false, panel = "overview", overview = null, runs = [], report = null;
  let selectedRun = "", lastSuccess = null;
  const esc = (v) => String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const bytes = (v) => v === null || v === undefined ? "未采集" : (v / 1048576).toFixed(1) + " MiB";
  const signed = (v) => v === null || v === undefined ? "未采集" : (v >= 0 ? "+" : "") + bytes(v);
  const date = (t) => t ? new Date(t * 1000).toLocaleString() : "未采集";
  const $ = (id) => root.querySelector("#ob-" + id);
  const phaseNames = {import:"导入",construct:"构造",initialize:"初始化",startup_window:"日志窗口"};
  const classNames = {insufficient_data:"采样不足",single_trial:"初步结果，需重复",environment_changed:"环境已变化，不宜直接归因",within_variation:"差异未超过自然波动",difference_observed:"重复观测到差异"};

  root.innerHTML = '<header class="ob-header"><div><h1>MemoryScope <span class="ob-mode">/ 服务器观察</span></h1><p>持续记录 · 启动归因 · 对照验证</p></div><div class="ob-tools"><span id="ob-status" class="ob-status">等待连接</span><button id="ob-theme" type="button">切换明暗</button><button id="ob-refresh" type="button">刷新</button></div></header>' +
    '<form id="ob-auth" class="ob-auth" '+(embedded?'hidden':'')+'><input id="ob-token" type="password" autocomplete="off" aria-label="后端访问凭据" placeholder="输入后端访问凭据，只保存在当前页面"><button type="submit">连接</button></form>' +
    '<div id="ob-error" class="ob-notice" role="status" hidden></div><nav class="ob-tabs" aria-label="观察报告"><button data-ob-panel="overview" aria-selected="true">总览</button><button data-ob-panel="startup" aria-selected="false">启动报告</button><button data-ob-panel="experiments" aria-selected="false">对照实验</button><button data-ob-panel="about" aria-selected="false">测量说明</button></nav>' +
    '<main id="ob-content"></main>';

  async function api(name, params, body) {
    if (embedded) {
      if (!bridge) throw new Error("AstrBot 页面桥接不可用");
      const result = body === undefined ? await bridge.apiGet("observer_" + name, params || {}) : await bridge.apiPost("observer_" + name, body);
      if (result && result.status === "error") throw new Error(result.message || "请求失败");
      return result && result.status === "ok" ? result.data : result;
    }
    const endpoint = name === "start" ? "experiments" : name;
    const query = new URLSearchParams(params || {}).toString();
    const response = await fetch("/api/" + endpoint + (query ? "?" + query : ""), {
      method:body === undefined ? "GET" : "POST", cache:"no-store", signal:AbortSignal.timeout(12000),
      headers:{Authorization:"Bearer " + token,"Content-Type":"application/json"},
      body:body === undefined ? undefined : JSON.stringify(body)
    });
    const result = await response.json();
    if (!response.ok || result.status !== "ok") throw new Error(result.error || "后端请求失败");
    return result.data;
  }

  function table(heads, rows) {
    return '<div class="ob-table-wrap"><table><thead><tr>'+heads.map(h=>'<th>'+esc(h)+'</th>').join('')+'</tr></thead><tbody>'+rows.map(row=>'<tr>'+row.map(v=>'<td>'+esc(v)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>';
  }
  function card(label,value,caption) { return '<article class="ob-card"><span>'+esc(label)+'</span><strong>'+esc(value)+'</strong><small>'+esc(caption)+'</small></article>'; }
  function box(title, content) { return '<section class="ob-box"><h2>'+esc(title)+'</h2>'+content+'</section>'; }
  function chart(samples) {
    const valid = samples.filter(s=>s.memory && s.memory.anon_swap !== null && s.memory.anon_swap !== undefined);
    if (valid.length < 2) return '<p class="ob-empty">等待足够的曲线采样</p>';
    const width=Math.max(300,Math.min(960,root.clientWidth-50)), left=44, right=width-16;
    const start=valid[0].ts, duration=Math.max(1,valid[valid.length-1].ts-start), max=Math.max(1,...valid.map(s=>s.memory.anon_swap));
    const path = key => valid.filter(s=>s.memory[key] !== null && s.memory[key] !== undefined).map((s,i)=>(i?'L':'M')+(left+((s.ts-start)/duration)*(right-left)).toFixed(2)+','+(205-s.memory[key]/max*175).toFixed(2)).join(' ');
    let lines='';
    for(let i=0;i<4;i++) { const y=205-i*175/3; lines+='<line class="grid" x1="'+left+'" x2="'+right+'" y1="'+y+'" y2="'+y+'"/><text x="2" y="'+(y+4)+'">'+(max*i/3/1048576).toFixed(0)+'</text>'; }
    return '<svg class="ob-chart" viewBox="0 0 '+width+' 240" role="img" aria-label="匿名内存加 Swap 趋势，单位 MiB">'+lines+'<path class="curve" d="'+path('anon_swap')+'"/><path class="swap-curve" d="'+path('swap')+'"/><text x="'+left+'" y="230">'+esc(new Date(start*1000).toLocaleTimeString())+'</text><text x="'+(right-75)+'" y="230">'+esc(new Date(valid[valid.length-1].ts*1000).toLocaleTimeString())+'</text></svg><p class="ob-caption">绿色：匿名内存＋Swap · 黄色：Swap · MiB。两条曲线不能再相加。</p>';
  }
  function renderOverview() {
    const l=overview.latest||{}, m=l.memory||{}, own=overview.observer||{}, h=l.host||{};
    let out='<div class="ob-cards">'+card('服务物理记账',bytes(m.current),'含文件缓存、内核记账等')+card('匿名内存＋Swap',bytes(m.anon_swap),'与上项有重叠，不能相加')+card('服务器可用内存',bytes(h.available),'与整机压力一起判断')+card('采集器自身 RSS',bytes(own.rss),'独立计量，不计入 AstrBot 服务')+'</div>';
    out+=box('近期趋势',report?chart(report.samples||[]):'<p class="ob-empty">尚无运行批次</p>');
    out+=box('服务进程',table(['PID / 名称','启动关联插件','RSS','Swap','PSS','PSS 采样时间'],(l.processes||[]).map(p=>[p.pid+' / '+p.name,p.startup_owner||'未归因',bytes(p.rss),bytes(p.swap),bytes(p.pss),date(p.detailed_at)]))+'<p class="ob-caption">进程 RSS 含共享页，不能简单求和作为服务总占用。PSS 为低频采样；短命进程可能未被采到。</p>');
    out+=box('采集开销与来源','<p>累计 CPU：'+Number(own.cpu_seconds||0).toFixed(2)+' 秒 · 平均采样：'+(own.sampling_count?own.sampling_ms/own.sampling_count:0).toFixed(2)+' ms · 最慢一次：'+Number(own.max_sampling_ms||0).toFixed(2)+' ms</p><p class="ob-source">'+esc((overview.run||{}).id||'等待 AstrBot 启动')+'</p><p class="ob-caption">源：独立后端 / cgroup v2 · 数据库丢弃：'+esc(own.history_dropped||0)+' · 内存压力：'+esc(m.pressure||'不可用')+'</p>');
    $('content').innerHTML=out;
  }
  function runOptions() { return runs.map(r=>'<option value="'+esc(r.id)+'" '+(r.id===selectedRun?'selected':'')+'>'+esc(date(r.started_at)+' · PID '+r.pid+(r.plan&&r.plan.label?' · '+r.plan.label:''))+'</option>').join(''); }
  function renderStartup() {
    let out='<div class="ob-tools"><label for="ob-run">启动批次</label><select id="ob-run">'+runOptions()+'</select><button id="ob-export">导出报告</button></div>';
    if (!report) { $('content').innerHTML=out+'<p class="ob-empty">尚无启动报告</p>';return; }
    out+='<p class="ob-caption">探针记录：'+(report.probe_complete?'完整':'不完整 / 未启用')+' · 记录开销：'+(report.probe_overhead_ms===null?'未知':Number(report.probe_overhead_ms).toFixed(2)+' ms')+' · 缺失事件：'+esc(report.missing_events===null?'未知':report.missing_events)+'</p>';
    out+=box('启动时间线',chart(report.samples||[]));
    const rows=(report.phases||[]).map(p=>[p.plugin,phaseNames[p.phase]||p.phase,((p.start-report.run.started_at)).toFixed(3)+' s',Number(p.duration_ms).toFixed(2)+' ms',signed(p.process_rss_swap_delta),signed(p.service_anon_swap_delta),p.failed?'异常':'已记录']);
    out+=box('逐插件加载阶段',table(['插件','阶段','相对启动','耗时','主进程 RSS＋Swap 增量','服务 anon＋Swap 增量','状态'],rows)+'<p class="ob-caption">所有增量均可能包含并发任务；不是独占内存。短于采样间隔的服务增量显示“未采集”。</p>');
    out+=box('启动时创建的子进程',table(['关联插件','子进程 PID','时间','来源'],(report.child_spawns||[]).map(c=>[c.plugin,c.child_pid,date(c.ts),'启动执行上下文']))+'<p class="ob-caption">只关联启动窗口内通过 Python subprocess 创建的进程；不会把同一时间出现的其他进程强行归因。</p>');
    const unmeasured=(report.plugins||[]).filter(p=>!p.phases.length);
    if(unmeasured.length) out+=box('未覆盖的插件',table(['插件','状态'],unmeasured.map(p=>[p.display_name||p.plugin,p.activated===false?'已禁用，未捕获边界':'未采集到启动边界'])));
    out+=box('本次新出现的依赖',table(['包名','首次观察到的导入者'],(report.packages||[]).map(p=>[p.name,p.first_importer]))+'<p class="ob-caption">此表记录依赖出现位置，不将共享库的内存全算给第一个插件；没有测量值就不填大小。</p>');
    $('content').innerHTML=out;
    $('run').onchange=async e=>{selectedRun=e.target.value;await refresh();};
    $('export').onclick=()=>{const blob=new Blob([JSON.stringify(report,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='memoryscope-startup.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  }
  async function renderExperiments() {
    const jobs=await api('experiments');
    const inventory=((overview.run||{}).inventory||[]).filter(p=>p.activated&&!p.reserved&&p.root_dir_name!=='astrbot_plugin_memory_scope');
    let out='<p class="ob-notice">对照实验会重启 AstrBot，期间机器人短暂离线。1 轮为 A → B → A，共 3 次重启；结果仍属于初步证据。不会删除插件或修改业务数据。</p>';
    out+='<form id="ob-experiment-form" class="ob-form"><label>目标插件<select id="ob-plugin">'+inventory.map(p=>'<option value="'+esc(p.root_dir_name)+'">'+esc(p.display_name||p.root_dir_name)+'</option>').join('')+'</select></label><label>对照方式<select id="ob-mode"><option value="skip">B：完全跳过加载</option><option value="disable">B：仅禁用实例和功能</option></select></label><label>轮数<select id="ob-rounds"><option>1</option><option>2</option><option>3</option></select></label><button type="submit" '+(!(overview.config||{}).allow_experiments?'disabled':'')+'>创建实验</button></form><p class="ob-caption">后端实验控制：'+((overview.config||{}).allow_experiments?'已启用':'未启用，请先配置 allow_experiments')+'。环境指纹不同、采样不足时不会给出可靠节省结论。</p>';
    out+=box('比较已有批次','<div class="ob-columns"><label>A 组（可多选）<select id="ob-group-a" multiple size="4">'+runOptions()+'</select></label><label>B 组（可多选）<select id="ob-group-b" multiple size="4">'+runOptions()+'</select></label></div><button id="ob-compare" type="button">比较所选批次</button><div id="ob-comparison"></div>');
    out+=box('实验记录',jobs.length?jobs.map(j=>'<article class="ob-job"><strong>'+esc(j.plugin)+' · '+esc(j.mode)+' · '+esc(j.state)+'</strong><p>'+esc(date(j.started_at))+' · '+esc((j.runs||[]).length)+' / '+esc(j.planned_restarts)+' 个采样批次</p><p>'+esc(j.message||'')+'</p>'+(j.result?'<p>条件节省量：'+esc(bytes(j.result.saving_bytes))+' · '+esc(classNames[j.result.classification]||j.result.classification)+'</p>':'')+'<p class="ob-caption">'+esc(j.restoration||'')+'</p></article>').join(''):'<p class="ob-empty">还没有对照实验</p>');
    out+='<button id="ob-cancel" type="button">停止当前实验并恢复正常启动</button>';
    $('content').innerHTML=out;
    $('experiment-form').onsubmit=async e=>{e.preventDefault();const rounds=Number($('rounds').value);if(!window.confirm('此实验将重启 AstrBot '+(2*rounds+1)+' 次，并临时改变 '+$('plugin').value+' 的加载方式。现在执行？'))return;try{await api('start',null,{plugin:$('plugin').value,mode:$('mode').value,rounds,confirm_restart:true});await refresh();}catch(err){showError(err);}};
    $('cancel').onclick=async()=>{try{await api('cancel',null,{});await refresh();}catch(err){showError(err);}};
    $('compare').onclick=async()=>{try{const ids=id=>Array.from($(id).selectedOptions).map(o=>o.value).join(',');const result=await api('compare',{A:ids('group-a'),B:ids('group-b')});$('comparison').textContent='条件节省量：'+bytes(result.saving_bytes)+' · '+(classNames[result.classification]||result.classification)+' · 自然波动：'+bytes(result.variability_bytes);}catch(err){showError(err);}};
  }
  function renderAbout() {
    $('content').innerHTML=box('三种证据，各回答一个问题','<p>独立采集器记录服务整体和进程变化；启动探针标记导入、构造与初始化；A/B/A 对照验证在当前插件组合下停用目标的条件收益。</p><p>anon＋Swap 包括换出量，不等于物理内存。PSS 按进程分摊共享页，也无法拆分同一进程里的所有 Python 插件。</p><p>普通禁用仍可能导入插件，因此“禁用”和“跳过加载”分别测量。共享依赖仍被其他插件使用时，停用目标不会释放那部分。</p>')+box('开销和边界','<p>后端独立采样；探针只在启动期间安装，不追踪每次内存分配。对象普查和引用图扫描仍在 AstrBot 内执行，可能造成停顿，须手动触发。</p><p>探针记录自身执行时间，不能单凭这个时间证明总启动速度没有变化。完整开销需要在相同环境中重复比较有、无探针的启动。</p><p>页面刷新读取已有数据。后端失联时显示过期状态；不把旧批次当成当前数据，也不把未测量显示为零。</p>');
  }
  function showError(err) { $('error').hidden=false;$('error').textContent=(err.message||String(err))+(lastSuccess?'；保留的是 '+date(lastSuccess)+' 的历史显示。':'');$('status').textContent='连接异常 / 数据可能过期'; }
  async function refresh() {
    if(busy)return;busy=true;$('refresh').disabled=true;
    try {
      overview=await api('overview');runs=await api('runs');
      if(!selectedRun&&runs.length)selectedRun=runs[0].id;
      const id=panel==='startup'?selectedRun:(overview.run||{}).id;
      if((panel==='overview'||panel==='startup')&&id)report=await api('run',{id});
      $('error').hidden=!overview.stale&&!overview.error;
      if(overview.stale||overview.error)$('error').textContent=overview.bridge_error||overview.error||'后端采样已过期';
      $('status').textContent=(overview.stale?'数据已过期':'已连接')+' · '+Number(overview.age_seconds||0).toFixed(1)+'s 前采样';
      if(panel==='overview')renderOverview();else if(panel==='startup')renderStartup();else if(panel==='experiments')await renderExperiments();else renderAbout();
      lastSuccess=Date.now()/1000;
    }catch(err){showError(err);}finally{busy=false;$('refresh').disabled=false;}
  }
  root.querySelectorAll('[data-ob-panel]').forEach(button=>button.onclick=()=>{panel=button.dataset.obPanel;root.querySelectorAll('[data-ob-panel]').forEach(b=>b.setAttribute('aria-selected',String(b===button)));if(panel==='about')renderAbout();else refresh();});
  $('refresh').onclick=refresh;
  $('theme').onclick=()=>{root.dataset.skin=root.dataset.skin==='light'?'dark':'light';};
  $('auth').onsubmit=e=>{e.preventDefault();token=$('token').value;$('token').value='';refresh();};
  window.MemoryScopeObserver={refresh};
  window.addEventListener('memoryscope:observer',refresh);
  setInterval(()=>{if(!document.hidden&&!busy&&!root.contains(document.activeElement)&&(!embedded||root.closest('.panel.is-active'))&&(embedded||token))refresh();},15000);
  if(embedded&&root.closest('.panel.is-active'))refresh();
})();
