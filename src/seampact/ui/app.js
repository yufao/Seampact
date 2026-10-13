const $ = id => document.getElementById(id);
const el = (name, text, className) => {
  const node = document.createElement(name);
  if (text !== undefined && text !== null) node.textContent = String(text);
  if (className) node.className = className;
  return node;
};
let state, selectedSource = null, selectedDetail = null, selectedRun = null;
let refreshing = false, starting = false, runSubmitting = false, inputsInitialized = false;
const sourceInfo = {
  'mmct-image': {icon:'IMG', title:'MMCT ImageAgent', note:'图像检查与工具调用', description:'从图像 Agent、模型接口和检查工具中发现能力。完整图像任务的运行接入仍待完成。'},
  'crag-text': {icon:'RAG', title:'LlamaIndex CRAG', note:'检索、评价与回答', description:'从原 CRAG 工作流中发现检索、文档评价、查询改写和回答能力。已提供可运行的 CRAG 任务示例。'},
  'abc-policy': {icon:'ABC', title:'NeMo ABC Bot', note:'政策、门控与知识库', description:'一起分析配置、规则、提示词与知识库，追踪它们依赖的原生方法。完整机器人运行接入仍待完成。'},
  'disp-offline': {icon:'DiSP', title:'DiSP', note:'数据净化与离线作业', description:'分析净化、训练和评估脚本中的能力，以及模型和数据依赖。真实离线作业的运行接入仍待完成。'}
};
const statusText = {
  RUNNING:'运行中', COMPLETED:'已完成', CANCELLING:'正在停止', CANCELLED:'已停止',
  INTERRUPTED:'已中断', FAILED:'未完成', PASS:'通过', FAIL:'未通过', NOT_RUN:'尚未运行',
  INVALID:'记录无效', BUILT_DRAFT:'已生成草案'
};
const modeText = {W:'包装原方法',H:'原生接线',R:'局部提取'};
function notice(text) { $('notice').textContent = text; $('notice').hidden = false; }
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw Error(data.error || '操作未完成');
  return data;
}
function button(text, action, secondary = false) {
  const node = el('button', text, secondary ? 'secondary' : '');
  node.type = 'button';
  node.onclick = async () => {
    node.disabled = true;
    try { await action(); await refresh(); } catch (error) { notice(error.message); }
    finally { if (node.isConnected) node.disabled = false; }
  };
  return node;
}
function tag(text, style = '') { return el('span', text, 'tag ' + style); }
function statusStyle(status) {
  return ['PASS','COMPLETED','BUILT_DRAFT'].includes(status) ? 'pass' :
    ['FAILED','FAIL','INVALID'].includes(status) ? 'fail' :
    ['RUNNING','CANCELLING'].includes(status) ? 'running' : 'draft';
}
function newest(items) { return [...(items || [])].sort((a,b) => (b.started_at || b.created_at || '').localeCompare(a.started_at || a.created_at || '')); }
function sourceForAnalysis(job) { return (state.requests || []).find(request => request.id === job.request_id)?.source_id; }
function jobsForSource() { return newest(state.analyses).filter(job => sourceForAnalysis(job) === selectedSource); }
function packagesForSource() {
  const jobIds = new Set(jobsForSource().map(job => job.id));
  return (state.packages || []).filter(pkg => jobIds.has(pkg.analysis_id));
}
function readyProfiles() { return (state.profiles || []).filter(profile => profile.key_usable && profile.model); }
function fillProviderSelect(node, disabledUnavailable = true) {
  const previous = node.value;
  node.replaceChildren();
  for (const profile of state.profiles || []) {
    const option = el('option', profile.provider.toUpperCase() + (profile.key_usable && profile.model ? '' : ' · 未配置'));
    option.value = profile.provider;
    option.disabled = disabledUnavailable && (!profile.key_usable || !profile.model);
    node.append(option);
  }
  node.value = readyProfiles().some(profile => profile.provider === previous) ?
    previous : readyProfiles()[0]?.provider || 'deepseek';
}
function sourceCards() {
  const root = $('source-cards'); root.replaceChildren();
  for (const source of state.sources || []) {
    const info = sourceInfo[source.id] || {icon:'SRC', title:source.name || source.id, note:'完整 Harness 来源', description:source.entry};
    const card = el('button', undefined, 'card source-card' + (source.id === selectedSource ? ' selected' : ''));
    card.type = 'button'; card.dataset.source = source.id;
    card.setAttribute('aria-pressed', String(source.id === selectedSource));
    card.append(el('span', info.icon, 'source-icon'), el('span', info.title, 'source-name'), el('p', info.note));
    const ownJobs = (state.analyses || []).filter(job => sourceForAnalysis(job) === source.id);
    const ownPackages = (state.packages || []).filter(pkg => ownJobs.some(job => job.id === pkg.analysis_id));
    card.append(tag((source.files || []).length + ' 份源码'));
    if (ownPackages.length) card.append(tag(ownPackages.length + ' 份适配草案', 'pass'));
    else if (ownJobs.some(job => job.status === 'RUNNING')) card.append(tag('分析中', 'running'));
    else if (ownJobs.length) card.append(tag('已分析', 'draft'));
    card.onclick = () => { selectedSource = source.id; renderWorkbench(); renderExecution(); sourceCards(); };
    root.append(card);
  }
  if (!root.children.length) root.append(el('p', '尚未登记来源。启动服务时指定 sources.json 即可加载。', 'empty-note'));
}
function renderWorkbench() {
  const source = (state.sources || []).find(item => item.id === selectedSource);
  const jobs = jobsForSource();
  const packages = packagesForSource();
  $('selected-source-title').textContent = source ? (sourceInfo[source.id]?.title || source.name || source.id) + ' · 自动拆分' : '自动分析与适配';
  $('source-description').textContent = sourceInfo[source?.id]?.description || source?.entry || '选择来源后准备源码并开始分析。';
  const stage = packages.length ? '适配草案已生成' : jobs.some(job => job.status === 'RUNNING') ? 'API 正在分析' : jobs.length ? '查看分析结果' : '等待开始';
  $('source-stage').textContent = stage;
  $('source-stage').className = 'tag ' + (packages.length ? 'pass' : jobs.some(job => job.status === 'RUNNING') ? 'running' : '');
  fillProviderSelect($('analysis-provider'));
  const busy = starting || jobs.some(job => ['RUNNING','CANCELLING'].includes(job.status));
  $('start-analysis').disabled = !source || !readyProfiles().length || busy;
  $('freeze-source').disabled = !source || starting;
  $('start-analysis').textContent = starting ? '正在准备…' : '准备来源并开始分析';
  $('analysis-start-help').textContent = !readyProfiles().length ? '先在下方配置一个模型接口；也可以先运行受控预览。' : busy ? '分析会持续读取源码并检查候选。进度在下方更新。' : '开始后会调用所选 API，沿用提供方的模型能力。';

  const requests = $('requests'); requests.replaceChildren();
  const ownRequests = (state.requests || []).filter(request => request.source_id === selectedSource);
  for (const request of ownRequests.slice(0,3)) {
    const row = el('div', undefined, 'row');
    row.append(el('span', '已准备源码 · ' + request.id.slice(0,8) + ' · ' + request.files.length + ' 份文件', 'muted small'));
    if (!(state.analyses || []).some(job => job.request_id === request.id && ['RUNNING','CANCELLING'].includes(job.status))) {
      const start = button('从这份源码开始', async () => {
        await api('/api/analyze', {request_id:request.id, provider:$('analysis-provider').value});
        notice('分析已开始，页面会自动更新。');
      }, true);
      start.disabled = !readyProfiles().length;
      row.append(start);
    }
    requests.append(row);
  }
  analysisCards(jobs, packages);
}
function analysisCards(jobs, packages) {
  const root = $('analysis-cards'); root.replaceChildren();
  for (const job of jobs) {
    const card = el('div', undefined, 'card analysis-card');
    const head = el('div', undefined, 'section-head');
    const title = el('div'); title.append(el('h3', job.provider.toUpperCase() + ' 分析 · ' + job.id.slice(0,8)), el('p', (job.rounds || 0) + ' 轮模型响应', 'muted small'));
    head.append(title, tag(statusText[job.status] || job.status, statusStyle(job.status))); card.append(head);
    if (job.error) card.append(el('p', job.error, 'muted small'));
    const candidate = job.submitted?.candidate;
    const report = job.submitted?.report;
    if (candidate?.boundaries?.length) {
      const list = el('div', undefined, 'candidate-list');
      for (const boundary of candidate.boundaries) {
        const checked = (report?.boundaries || []).find(item => item.id === boundary.id);
        const row = el('div', undefined, 'candidate-row');
        const name = el('div'); name.append(el('strong', boundary.id, 'candidate-name'), el('div', boundary.symbol || boundary.rationale, 'muted'));
        row.append(name, tag(modeText[boundary.mode] || boundary.mode), tag(checked?.buildable ? '可生成适配' : '需要补充接线', checked?.buildable ? 'pass' : 'draft'));
        list.append(row);
      }
      card.append(list);
    }
    const actions = el('div', undefined, 'actions');
    actions.append(button('查看分析记录', async () => {
      selectedDetail = job.id;
      $('detail').textContent = JSON.stringify(await api('/api/analysis/' + job.id), null, 2);
      $('analysis-detail').open = true;
    }, true));
    if (job.status === 'RUNNING') actions.append(button('停止分析', async () => {
      await api('/api/cancel', {id:job.id}); notice('当前请求结束后停止，不再发起下一轮。');
    }, true));
    if (job.status === 'COMPLETED' && candidate?.boundaries?.length) {
      const existing = packages.find(pkg => pkg.analysis_id === job.id);
      if (!existing) {
        const build = button('生成适配草案', async () => {
          await api('/api/build', {id:job.id}); notice('适配草案已生成，可以下载或在已支持的宿主中运行。');
        });
        const buildable = report?.boundaries?.length && report.boundaries.every(boundary => boundary.buildable);
        build.disabled = !buildable; actions.append(build);
        if (!buildable) card.append(el('p', '部分候选仍缺少可实现的接线信息。完整记录保留了检查原因。', 'muted small'));
      }
    }
    card.append(actions); root.append(card);
  }
  if (!jobs.length) root.append(el('p', 'API 会根据准则读取源码和相关依赖，提出能力边界；无需手工预填拆分方案。', 'empty-note'));
  const packageRoot = $('packages'); packageRoot.replaceChildren();
  for (const pkg of packages) {
    const row = el('div', undefined, 'row');
    const description = el('div');
    description.append(el('strong', pkg.candidate.boundaries.length + ' 个能力 · 适配草案'), el('p', pkg.id.slice(0,8) + ' · 包含原始源码、适配代码和检查记录', 'muted small'));
    const actions = el('div', undefined, 'actions');
    const link = el('a', '下载 ZIP', 'download'); link.href = '/api/package/' + pkg.id; link.download = pkg.archive; actions.append(link);
    if (selectedSource === 'crag-text') actions.append(button('使用这份草案运行', () => {
      renderExecution(); $('run-package').value = 'package:' + pkg.id;
      $('execution').scrollIntoView({behavior:'smooth',block:'start'});
    }, true));
    row.append(description, actions); packageRoot.append(row);
  }
}
async function startSource(analysis) {
  if (starting || !selectedSource) return;
  starting = true; renderWorkbench();
  try {
    const request = await api('/api/freeze', {source_id:selectedSource, observation:$('observation').value});
    if (analysis) {
      await api('/api/analyze', {request_id:request.id, provider:$('analysis-provider').value});
      notice('源码已准备，API 正在分析能力边界。');
    } else notice('源码已准备，可以从这份源码开始分析。');
  } catch (error) { notice(error.message); }
  finally { starting = false; await refresh(); }
}
function executionProfile() {
  return (state.execution?.profiles || []).find(profile => profile.source_id === selectedSource);
}
function executionReason(profile) {
  const reason = profile?.reason || '';
  if (reason.includes('install the optional CRAG SDK')) return '当前应用环境尚未安装 CRAG 运行依赖，请按部署说明启用 CRAG 任务环境。';
  if (reason.includes('--artifacts') || reason.includes('artifact is missing')) return '当前服务尚未加载已有 CRAG 适配草案。也可以在此平台分析源码，生成并选用新的草案。';
  return reason ? '运行环境或草案尚未就绪，请核对部署配置。' : '当前服务还未配置 CRAG 运行环境和适配草案。';
}
function renderExecution() {
  const profile = executionProfile();
  const previous = $('run-package').value;
  const select = $('run-package'); select.replaceChildren();
  if (profile?.ready) {
    const preset = el('option', '已提供的 CRAG API 适配草案'); preset.value = 'preset:' + profile.id; select.append(preset);
  }
  if (selectedSource === 'crag-text') for (const pkg of packagesForSource()) {
    const option = el('option', pkg.id.slice(0,8) + ' · ' + pkg.candidate.boundaries.length + ' 个能力'); option.value = 'package:' + pkg.id; select.append(option);
  }
  if ([...select.options].some(option => option.value === previous)) select.value = previous;
  fillProviderSelect($('run-provider'));
  const mode = $('run-mode').value;
  $('run-provider').disabled = mode !== 'live';
  $('run-mode-help').textContent = mode === 'live' ? '真实模型会调用你配置的接口。资料之间用空行分隔；检索限于这些资料，不接入外部搜索。' : '受控预览不调用模型、不需要 GPU；用于查看接线和执行，预览回答不代表真实模型效果。';
  const hostReady = Boolean(profile?.ready || (select.value.startsWith('package:') && state.execution?.sdk_available));
  const enabled = Boolean(hostReady && select.options.length && (mode === 'preview' || readyProfiles().length));
  $('run-workflow').disabled = !enabled || runSubmitting || (state.runs || []).some(run => run.status === 'RUNNING');
  $('run-workflow').textContent = runSubmitting ? '正在启动…' : '运行工作流 →';
  $('execution-status').textContent = hostReady ? 'CRAG 运行宿主已就绪' : '运行接入待完成';
  $('execution-status').className = 'tag ' + (hostReady ? 'pass' : 'draft');
  $('execution-help').textContent = !profile ? '当前来源可以分析与生成草案，任务运行宿主仍待接入。选择 CRAG 查看已实现的任务示例。' : !hostReady ? executionReason(profile) : '沿用原 CRAG SDK 的流程与能力接线。当前调度保留原流程依赖，检索范围为本次提供的资料。';
  if (!inputsInitialized && profile?.input_defaults) {
    $('run-question').value = profile.input_defaults.question || '';
    $('run-documents').value = (profile.input_defaults.documents || []).join('\n\n');
    inputsInitialized = true;
  }
  const runs = newest(state.runs);
  const run = runs.find(item => item.id === selectedRun) || runs[0];
  if (run && selectedSource === 'crag-text') renderRun(run);
  else if (selectedSource !== 'crag-text') {
    const output = $('run-result'); output.replaceChildren();
    const empty = el('div', undefined, 'empty-result'); empty.append(el('span','↗','empty-icon'),el('h3','这个来源的运行宿主待接入'),el('p','源码分析与适配生成可以继续使用。CRAG 已提供可运行任务示例。')); output.append(empty);
  } else if (!run) {
    const output = $('run-result'); output.replaceChildren();
    const empty = el('div', undefined, 'empty-result'); empty.append(el('span','↗','empty-icon'),el('h3','看见一次完整运行'),el('p','运行后在这里查看回答、被激活的能力和执行顺序。')); output.append(empty);
  }
  const history = $('run-history-list'); history.replaceChildren();
  for (const item of runs.slice(0,10)) {
    const row = el('div', undefined, 'row'); row.append(el('span',item.id.slice(0,8) + ' · ' + (item.mode === 'preview' ? '受控预览' : '真实模型')), tag(statusText[item.status] || item.status,statusStyle(item.status)));
    row.append(button('查看', () => { selectedRun = item.id; renderRun(item); }, true)); history.append(row);
  }
  $('run-history').hidden = !runs.length;
}
function renderRun(run) {
  const root = $('run-result'); root.replaceChildren();
  const report = run.report || run.result || {};
  const head = el('div', undefined, 'result-head');
  head.append(el('h3', run.mode === 'preview' ? '受控预览结果' : '任务回答'),tag(statusText[run.status] || run.status,statusStyle(run.status))); root.append(head);
  if (run.question) root.append(el('p', run.question, 'run-scope'));
  if (run.status === 'RUNNING') {
    const progress = run.progress || {};
    const phases = {native_setup:'准备原生工作流',native_dispatch:'调度原生调用',relevance:'判断资料相关性',query_transform:'改写检索问题',answer:'生成回答',completed:'完成'};
    const text = (phases[progress.phase] || '正在执行原 CRAG 工作流') + ' · 已完成模型调用 ' + (progress.completed_model_calls || 0) + ' 次';
    const note = el('p', undefined, 'muted'); note.append(el('span',undefined,'spinner'),document.createTextNode(text)); root.append(note);
  }
  if (run.error) root.append(el('p', typeof run.error === 'string' ? run.error : JSON.stringify(run.error), 'muted'));
  if (report.answer !== undefined) root.append(el('div', typeof report.answer === 'string' ? report.answer : JSON.stringify(report.answer,null,2), 'answer'));
  const modules = report.activated_modules || [];
  if (modules.length) {
    root.append(el('p','本次激活的能力','module-caption'));
    const path = el('div', undefined, 'module-path');
    for (const module of modules) {
      const noop = typeof module === 'object' && module.native_branch === 'NOOP_START_EVENT_BRANCH';
      const chip = el('span', typeof module === 'string' ? module : module.id + (module.calls ? ' ×' + module.calls : '') + (noop ? '（空分支）' : ''), 'module-chip');
      if (typeof module === 'object') chip.title = module.symbol || module.id;
      path.append(chip);
    }
    root.append(path);
  }
  const scheduler = report.scheduler || {};
  if (run.status === 'COMPLETED') root.append(el('p', '调度：' + (scheduler.policy || 'FIFO').toUpperCase() + ' · ' + (scheduler.parallel ? '并行已启用' : '沿用原流程依赖顺序'), 'run-scope'));
  if (report.model_calls !== undefined) {
    const count = Array.isArray(report.model_calls) ? report.model_calls.length : report.model_calls;
    root.append(el('p', '本次模型调用 ' + count + ' 次 · 检索限于输入资料', 'run-scope'));
  }
  if (run.mode === 'preview') root.append(el('p', '模型和检索响应受控，仅用于展示流程。', 'run-scope'));
  const detail = el('details'); detail.append(el('summary','本次执行明细'),el('pre',JSON.stringify(run,null,2))); root.append(detail);
}
function evidenceCards() {
  const root = $('evidence-cards'); root.replaceChildren();
  const names = {'mmct-sdk':'MMCT 原生 SDK','crag-fragment':'CRAG 局部提取','crag-sdk':'CRAG 原生 SDK','docker':'Docker 部署','crag-api':'CRAG API 草案运行'};
  for (const record of state.evidence?.records || []) {
    if (!names[record.id]) continue;
    const card = el('div',undefined,'card evidence-card'); card.dataset.evidence = record.id;
    card.append(el('h3',names[record.id]),tag(statusText[record.status] || record.status,statusStyle(record.status)));
    if (record.cases) card.append(el('p',record.cases + ' 个受控案例' + (record.boundary_count ? ' · '+record.boundary_count+' 个边界' : ''),'muted small'));
    const versions = Object.entries(record.sdk || {}).map(([name,version]) => name+' '+version);
    if (versions.length) card.append(el('p',versions.join(' · '),'sdk-versions'));
    card.append(el('p',record.id === 'docker' ? '范围以容器记录为准。' : '受控机制运行；真实模型效果另外记录。','muted small'));
    const details = el('details'); details.append(el('summary','查看记录'),el('pre',JSON.stringify(record,null,2))); card.append(details); root.append(card);
  }
}
function models() {
  const root = $('model-cards');
  for (const profile of state.profiles || []) {
    let card = [...root.children].find(item => item.dataset.provider === profile.provider);
    if (card) { card.updateProfile(profile); continue; }
    card = el('form',undefined,'card'); card.dataset.provider = profile.provider; card.append(el('h3',profile.provider.toUpperCase()));
    const help = el('p',undefined,'muted credential-help'); card.append(help);
    function field(label,type,value) {
      const wrapper = el('label',label), input = el('input');
      input.type = type; input.value = value || ''; wrapper.append(input); card.append(wrapper); return input;
    }
    const base = field('接口地址','url',profile.base_url), model = field('模型 ID','text',profile.model);
    const key = field('API Key','password',''), max = field('最大输出能力（可选；优先自动发现）','number',profile.declared_max_output_tokens);
    key.autocomplete = 'new-password';
    const capabilities = el('p',undefined,'muted'); card.append(capabilities);
    const save = button('保存接口',async () => {
      const body = {provider:profile.provider,base_url:base.value,model:model.value,max_output_tokens:max.value ? Number(max.value) : null};
      if (!key.disabled) body.api_key = key.value;
      await api('/api/profile',body); key.value = ''; notice('接口已保存。');
    },true);
    const discover = button('发现模型能力',async () => {
      const data = await api('/api/discover',{provider:profile.provider});
      capabilities.textContent = JSON.stringify(data.selected || {note:'接口未提供选定模型的能力信息'});
    },true);
    card.append(save,document.createTextNode(' '),discover);
    card.updateProfile = latest => {
      const localInput = latest.credential_mode === 'windows_dpapi'; key.disabled = !localInput;
      if (!localInput) {
        key.value = ''; key.placeholder = latest.key_usable ? '已通过私有配置加载' : '尚未通过私有配置加载';
        help.textContent = '服务器使用私有密钥文件配置 Key。本页保存接口地址和模型参数；密钥配置方法见部署说明。';
      } else {
        key.placeholder = latest.key_usable ? '已配置，留空保持' : '填写后按当前 Windows 用户加密保存';
        help.textContent = latest.credential_error || 'Key 按当前 Windows 用户加密保存，留空保留已有凭据。';
      }
      discover.disabled = !latest.key_usable;
      const selected = latest.capabilities?.selected;
      capabilities.textContent = selected ? '上下文 '+(selected.context_window ?? '未知')+' / 最大输出 '+(selected.max_output_tokens ?? '未知') :
        latest.provider === 'glm' ? '填入你的 GLM 模型 ID 后可发现接口能力。' : '配置凭据后可发现接口能力。';
    };
    card.updateProfile(profile); card.onsubmit = event => event.preventDefault(); root.append(card);
  }
}
function renderRuntime() {
  $('quota').textContent = state.runtime?.quota ?? '—'; $('used').textContent = state.runtime?.used ?? '—';
  $('calls').textContent = Object.keys(state.runtime?.calls || {}).length;
  const root = $('demo-results'); root.replaceChildren();
  for (const demo of (state.demos || []).slice(0,3)) {
    const row = el('div',undefined,'row'); row.append(el('span','教学流程 '+demo.id.slice(0,8)),tag(statusText[demo.status] || demo.status,statusStyle(demo.status))); root.append(row);
  }
}
async function refresh() {
  if (refreshing) return; refreshing = true;
  try {
    state = await api('/api/state');
    if (!(state.sources || []).some(source => source.id === selectedSource)) selectedSource = (state.sources || []).some(source => source.id === 'crag-text') ? 'crag-text' : state.sources?.[0]?.id;
    $('connection').textContent = '已连接'; $('version').textContent = '核心 '+state.version;
    sourceCards(); renderWorkbench(); renderExecution(); evidenceCards(); models(); renderRuntime();
    if ($('events').parentElement.open) $('events').textContent = JSON.stringify(await api('/api/events'),null,2);
    if (selectedDetail && $('analysis-detail').open) $('detail').textContent = JSON.stringify(await api('/api/analysis/'+selectedDetail),null,2);
  } catch (error) { $('connection').textContent = '连接中断'; notice(error.message); }
  finally { refreshing = false; }
}
$('start-analysis').onclick = () => startSource(true);
$('freeze-source').onclick = () => startSource(false);
$('quick-run').onclick = () => {
  if ((state?.sources || []).some(source => source.id === 'crag-text')) {
    selectedSource = 'crag-text'; sourceCards(); renderWorkbench(); renderExecution();
  }
};
$('run-mode').onchange = renderExecution;
$('run-package').onchange = renderExecution;
$('run-form').onsubmit = async event => {
  event.preventDefault(); if (runSubmitting) return;
  runSubmitting = true; renderExecution();
  try {
    const selection = $('run-package').value, source = selection.startsWith('package:') ? {package_id:selection.slice(8)} : {preset:selection.slice(7)};
    const response = await api('/api/run',{...source,mode:$('run-mode').value,provider:$('run-provider').value,
      question:$('run-question').value,documents:$('run-documents').value.split(/\n\s*\n/).map(text => text.trim()).filter(Boolean)});
    selectedRun = response.id; notice('工作流已开始。回答和实际能力调用会在右侧更新。');
  } catch (error) { notice(error.message); }
  finally { runSubmitting = false; await refresh(); }
};
$('demo').onclick = async () => { try { await api('/api/demo',{}); await refresh(); } catch (error) { notice(error.message); } };
$('events').parentElement.addEventListener('toggle',refresh);
refresh(); setInterval(refresh,3000);
