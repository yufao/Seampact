const $ = id => document.getElementById(id);
const node = (name, text, className) => {
  const element = document.createElement(name);
  if (text !== undefined && text !== null) element.textContent = String(text);
  if (className) element.className = className;
  return element;
};

const messages = [];
let state = null;
let pendingTask = null;
let refreshing = false;
let connected = false;
const analysisSubmitting = new Set();
const sourceUpdating = new Set();
const inspectPage = location.pathname === '/inspect';
let inspectQuery = new URLSearchParams(location.search);
let inspectView = inspectQuery.get('view') || 'analyses';
const viewNames = {analyses: '来源与分析', units: '能力池', tasks: '任务详情', scheduler: '调度器'};
if (!viewNames[inspectView]) inspectView = 'analyses';
const renderedSections = new Map();
const detailStates = new Map();
const terminalStatuses = new Set(['COMPLETED', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED', 'BLOCKED']);
const statusNames = {
  RUNNING: '运行中', QUEUED: '排队中', WAITING: '等待中', COMPLETED: '已完成',
  SUCCEEDED: '已完成', FAILED: '失败', CANCELLED: '已取消', INTERRUPTED: '已中断',
  READY: '源码已绑定', AVAILABLE: '可用', BLOCKED: '待接入', PENDING: '待确认',
  THEORY_PENDING: '理论待确认', IDLE: '空闲', ANALYZING: '分析中', ANALYZED: '已分析',
  REGISTERED: '已登记', CONNECTED: '已接入', MISSING: '待接入', NOT_CONFIGURED: '未配置', ENABLED: '已使能',
  AUTHORIZED: '已派发', RESERVED: '已占槽', UNKNOWN: '状态未知',
  STARTED: '执行中', RETIRED: '已结束', BUILT_DRAFT: '适配已生成',
  SOURCE_BOUND: '源码绑定已核对', ADMITTED: '操作条件成立', NOT_RUN: '尚未运行', BOUND: '原生绑定已执行',
  UNRESOLVED: '语义条件未决', RETURNED: '正常返回', STOPPED_AT_NON_SUCCESS_EXIT: '后继已停止',
  CHECKED_WITH_RUNTIME_VALUE_CONDITIONS: '结构已核对 · 按实际值检查输入', SOURCE_CHECKED: '源码已核对',
  DETACHED: '已摘除', SUPERSEDED: '已替换'
};

function inspectLink(view, parameters = {}) {
  const query = new URLSearchParams({view, ...parameters});
  const link = node('a', '查看完整详情', 'detail-link');
  link.href = '/inspect?' + query;
  link.target = '_blank';
  link.rel = 'noopener';
  return link;
}

function detailKey(detail, root) {
  const owner = detail.closest('[data-item-id]')?.dataset.itemId || root.id;
  const labels = [];
  for (let current = detail; current && current !== root; current = current.parentElement) {
    if (current.tagName === 'DETAILS') labels.unshift(current.querySelector(':scope > summary')?.textContent.split(' · ')[0] || '详情');
  }
  return root.id + '/' + owner + '/' + labels.join('/');
}

function renderSection(key, value, render, roots) {
  const signature = JSON.stringify(value);
  if (renderedSections.get(key) === signature) return;
  const focus = document.activeElement;
  let focusedKey = null;
  for (const id of roots) {
    const root = $(id);
    for (const detail of root.querySelectorAll('details')) {
      const identity = detailKey(detail, root);
      detailStates.set(identity, detail.open);
      if (focus === detail.querySelector(':scope > summary')) focusedKey = identity;
    }
  }
  const scroller = document.querySelector('.observer-scroll');
  const scrollTop = scroller.scrollTop;
  const configScroll = document.querySelector('.config-body').scrollTop;
  render();
  for (const id of roots) {
    const root = $(id);
    for (const detail of root.querySelectorAll('details')) {
      const identity = detailKey(detail, root);
      if (detailStates.has(identity)) detail.open = detailStates.get(identity);
      if (identity === focusedKey) detail.querySelector(':scope > summary')?.focus({preventScroll: true});
    }
  }
  scroller.scrollTop = scrollTop;
  document.querySelector('.config-body').scrollTop = configScroll;
  renderedSections.set(key, signature);
}

function selectedSource() { return inspectPage ? inspectQuery.get('harness') || '' : ''; }
function visibleUnits() {
  return (state.units || []).filter(unit => (!selectedSource() || unit.source_id === selectedSource()) &&
    (!inspectPage || !inspectQuery.get('unit') || unit.id === inspectQuery.get('unit')));
}
function visibleTasks() {
  const source = selectedSource();
  const sourceAnalyses = new Set((state.analyses || []).filter(job => job.harness_id === source).map(job => job.id));
  const sourceUnits = new Set((state.units || []).filter(unit => unit.source_id === source).map(unit => unit.id));
  return (state.tasks || []).filter(task =>
    (!inspectPage || !inspectQuery.get('task') || task.id === inspectQuery.get('task')) &&
    (!source || (task.calls || []).some(call => call.unit?.source_id === source) ||
      (task.plan?.units || []).some(id => sourceUnits.has(id) || sourceAnalyses.has(id.split(':')[0]))));
}

async function changeSource(harness) {
  if (sourceUpdating.has(harness.id)) return;
  const detached = harness.status === 'DETACHED';
  sourceUpdating.add(harness.id);
  try {
    await api('/api/harnesses/' + (detached ? 'restore' : 'detach'), {harness_id: harness.id});
    showNotice(detached ? '来源已重新接入，沿用已有分析，不调用分析模型。' :
      '来源已摘除，后续调用已停止；已开始的原调用可收尾，分析历史保留。');
  } catch (error) { showNotice(error.message); }
  finally { sourceUpdating.delete(harness.id); await refresh(); }
}

function sourceButton(harness) {
  const button = node('button', sourceUpdating.has(harness.id) ? '处理中…' :
    harness.status === 'DETACHED' ? '重新接入' : '摘除来源', 'secondary-button');
  button.type = 'button';
  button.disabled = sourceUpdating.has(harness.id) || analysisSubmitting.has(harness.id);
  button.onclick = () => {
    button.disabled = true;
    button.textContent = '处理中…';
    changeSource(harness);
  };
  return button;
}

function updateInspectPage() {
  if (!inspectPage) return;
  document.body.classList.add('inspect-page');
  document.title = 'SeamPact · ' + viewNames[inspectView];
  $('observer-title').textContent = viewNames[inspectView];
  $('inspect-description').hidden = false;
  $('inspect-filter').hidden = inspectView === 'scheduler';
  document.querySelector('.chat-shell').setAttribute('aria-hidden', 'true');
  document.querySelector('.rail-caption').textContent = '异构 Harness · 详情';
  for (const section of document.querySelectorAll('.observer-scroll > .panel-section')) section.hidden = section.dataset.view !== inspectView;
  for (const link of document.querySelectorAll('.rail-actions a[href^="/inspect"]')) {
    const view = new URL(link.href).searchParams.get('view');
    const query = new URLSearchParams({view});
    if (selectedSource() && view !== 'scheduler') query.set('harness', selectedSource());
    link.href = '/inspect?' + query;
    link.removeAttribute('target');
    link.classList.toggle('selected', view === inspectView);
  }
  const options = (state.harnesses || []).map(source => [source.id, source.name || source.address, source.status]);
  const selector = $('inspect-source');
  if (selector.dataset.options !== JSON.stringify(options)) {
    selector.replaceChildren(node('option', '全部来源'));
    selector.firstChild.value = '';
    for (const [id, name, status] of options) {
      const option = node('option', name + (status === 'DETACHED' ? ' · 已摘除' : ''));
      option.value = id;
      selector.append(option);
    }
    selector.dataset.options = JSON.stringify(options);
  }
  selector.value = selectedSource();
}

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '请求未完成');
  return result;
}

function textValue(value) {
  if (value === undefined || value === null) return '';
  return typeof value === 'string' ? value : JSON.stringify(value, null, 2);
}

function showNotice(text) {
  const notice = $(inspectPage ? 'inspect-notice' : 'notice');
  notice.textContent = text;
  notice.hidden = !text;
}

function statusClass(value) {
  const status = String(value || '').toUpperCase();
  if (['RUNNING', 'STARTED', 'ANALYZING', 'QUEUED'].includes(status)) return 'running';
  if (['FAILED', 'INTERRUPTED'].includes(status)) return 'error';
  if (['COMPLETED', 'SUCCEEDED', 'READY', 'AVAILABLE', 'ANALYZED', 'RETIRED', 'BUILT_DRAFT'].includes(status)) return 'ready';
  return 'pending';
}

function statusLabel(value) {
  return statusNames[String(value || '').toUpperCase()] || value || '未提供状态';
}

function pill(status) {
  return node('span', statusLabel(status), 'status-pill ' + statusClass(status));
}

function isReady(profile) {
  return Boolean(profile?.base_url && profile?.model && profile.ready !== false && profile.configured !== false);
}

function latency(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits: 1}) + ' ms' : null;
}

function entryCount(value) {
  if (typeof value === 'number') return value;
  if (Array.isArray(value)) return value.length;
  if (value && typeof value === 'object') return Object.keys(value).length;
  return null;
}

function entries(value) {
  if (Array.isArray(value)) return value;
  if (value && typeof value === 'object') return Object.values(value);
  return [];
}

function empty(root, text) {
  root.append(node('p', text, 'empty-note'));
}

function detailValue(root, label, value, open = false) {
  if (value === undefined || value === null) return;
  const detail = node('details', undefined, 'schema-note');
  detail.open = open;
  detail.append(node('summary', label), node('pre', textValue(value)));
  root.append(detail);
  return detail;
}

function renderProfile() {
  const profile = state.entry_profile;
  for (const id of ['entry-profile-summary', 'config-entry-profile']) {
    const root = $(id);
    if (!root) continue;
    root.replaceChildren();
    if (!profile) {
      empty(root, '平台尚未提供入口支持范围。');
      continue;
    }
    root.append(node('p', [profile.interpreter, profile.host === 'local-native-process' ? '本地原生进程' : profile.host,
      profile.explicit_lifetime ? '显式生命周期' : null,
      profile.frame_observation_excluded ? '不使用栈 / frame 反射' : null].filter(Boolean).join(' · '), 'card-note'));
    root.append(node('p', (profile.confirmed ? '已确认范围：' : '范围待确认：') + profile.id, 'card-meta'));
    detailValue(root, '宿主范围说明', profile);
  }
}

function responseText(response) {
  if (typeof response === 'string') return response;
  if (Array.isArray(response)) return response.map(item => typeof item === 'string' ? item : item.text ?? textValue(item)).join('\n');
  if (response?.content !== undefined) return responseText(response.content);
  if (response?.message !== undefined) return responseText(response.message);
  return textValue(response);
}

function appendMessage(role, text) {
  const welcome = $('conversation').querySelector('.welcome');
  if (welcome) welcome.remove();
  const item = node('article', undefined, 'chat-message ' + role);
  const content = node('div', undefined, 'message-content');
  const body = node('div', text, 'message-body');
  const meta = node('div', undefined, 'message-meta');
  content.append(node('div', role === 'user' ? '你' : 'SeamPact', 'message-role'), body, meta);
  item.append(node('div', role === 'user' ? '你' : 'S', 'message-avatar'), content);
  $('conversation').append(item);
  item.scrollIntoView({block: 'end', behavior: 'auto'});
  return {item, body, meta};
}

function updateComposer() {
  const ready = connected && isReady(state?.models?.chat);
  $('send-message').disabled = !ready || Boolean(pendingTask) || !$('chat-input').value.trim();
  $('send-message').querySelector('.send-label').textContent = pendingTask ? '运行中' : '发送';
  $('chat-model-name').textContent = isReady(state?.models?.chat) ? state.models.chat.model : '对话模型未配置';
  $('chat-model-name').className = 'status-pill ' + (isReady(state?.models?.chat) ? 'ready' : 'pending');
  const units = state?.units || [];
  const available = units.filter(unit => ['READY', 'AVAILABLE'].includes(String(unit.status).toUpperCase())).length;
  const hint = !connected ? '平台未连接。' : !isReady(state?.models?.chat) ?
    '先在配置中填写对话模型接口。' : available ?
    '对话已就绪 · 能力池中有 ' + available + ' 个源码已绑定入口。' :
    '对话已就绪 · 当前没有可调用 Unit；Harness 状态见右侧。';
  $('chat-readiness').textContent = hint;
  if ($('welcome-context')) $('welcome-context').textContent = hint;
}

async function sendMessage(event) {
  event.preventDefault();
  const content = $('chat-input').value.trim();
  if (!content || pendingTask || !connected || !isReady(state?.models?.chat)) return;
  messages.push({role: 'user', content});
  appendMessage('user', content);
  $('chat-input').value = '';
  resizeComposer();
  const output = appendMessage('assistant', '正在提交任务…');
  output.item.classList.add('message-pending');
  pendingTask = {id: null, output};
  updateComposer();
  showNotice('');
  try {
    const result = await api('/api/chat', {messages});
    if (!result.task_id) throw new Error('平台未返回任务 ID');
    pendingTask.id = result.task_id;
    output.body.textContent = '正在运行…';
    await refresh();
  } catch (error) {
    output.body.textContent = error.message;
    output.item.classList.remove('message-pending');
    output.item.classList.add('message-error');
    pendingTask = null;
    updateComposer();
  }
}

function synchronizeTask() {
  if (!pendingTask?.id) return;
  const task = (state.tasks || []).find(item => item.id === pendingTask.id);
  if (!task) return;
  const status = String(task.status || '').toUpperCase();
  const {output} = pendingTask;
  const text = responseText(task.response);
  const nearBottom = $('conversation').scrollHeight - $('conversation').scrollTop - $('conversation').clientHeight < 100;
  output.body.textContent = text || (task.error ? textValue(task.error) : statusLabel(task.status) + '…');
  const duration = latency(task.latency_ms);
  output.meta.textContent = [statusLabel(task.status), duration].filter(Boolean).join(' · ');
  if (terminalStatuses.has(status)) {
    output.item.classList.remove('message-pending');
    if (['COMPLETED', 'SUCCEEDED'].includes(status)) {
      if (text) messages.push({role: 'assistant', content: text});
      else output.body.textContent = '任务已完成，平台未返回对话内容。';
    } else {
      output.item.classList.add('message-error');
      if (text && task.error) output.meta.textContent += ' · ' + textValue(task.error);
    }
    pendingTask = null;
    updateComposer();
  }
  if (nearBottom) output.item.scrollIntoView({block: 'end', behavior: 'auto'});
}

function renderScheduler() {
  const scheduler = state.scheduler;
  const badge = $('scheduler-status');
  const root = $('scheduler-summary');
  root.replaceChildren();
  if (!scheduler) {
    badge.textContent = '未提供状态';
    badge.className = 'status-pill pending';
    empty(root, '平台尚未返回调度状态。');
    return;
  }
  badge.textContent = statusLabel(scheduler.status);
  badge.className = 'status-pill ' + statusClass(scheduler.status);
  root.append(node('p', scheduler.policy ? '策略：' + scheduler.policy : '策略尚未提供', 'card-meta'));
  const grid = node('div', undefined, 'state-grid');
  for (const [label, value] of [['执行中', scheduler.active], ['等待中', scheduler.waiting]]) {
    const count = entryCount(value);
    const stat = node('div', undefined, 'state-stat');
    stat.append(node('label', label), node('strong', count === null ? '—' : count));
    grid.append(stat);
  }
  root.append(grid);
  if (typeof scheduler.used === 'number' && typeof scheduler.quota === 'number') {
    root.append(node('p', '执行槽：' + scheduler.used + ' / ' + scheduler.quota, 'card-meta'));
  }
  if (scheduler.learning) {
    root.append(node('p', '原型使用两层表格 Q-learning，只选合法候选并按真实反馈更新。单候选执行不代表学到选择优势。', 'card-note'));
    const layers = node('div', undefined, 'rl-layers');
    for (const [key, label] of [['upper', '上层 · 方案选择'], ['lower', '下层 · 调用调度']]) {
      const learning = scheduler.learning[key];
      if (!learning) continue;
      const card = node('div', undefined, 'status-card');
      card.dataset.itemId = key;
      card.append(node('strong', label));
      const count = learning.candidate_count;
      card.append(node('p', '最近候选：' + (count === null || count === undefined ? '尚未选择' : count) + ' · 累计更新：' + (learning.updates ?? '—'), 'card-meta'));
      if (learning.action_key) {
        const selected = node('details');
        selected.append(node('summary', '最近选择'), node('pre', learning.action_key));
        card.append(selected);
      }
      layers.append(card);
    }
    root.append(layers);
  }
  if (scheduler.theory_pending) root.append(node('p', scheduler.theory_pending === true ?
    'RL 调度理论待确认。当前显示平台返回的执行状态。' : textValue(scheduler.theory_pending), 'theory-note'));
  for (const [label, value] of [['执行中', scheduler.active], ['等待中', scheduler.waiting]]) {
    const calls = entries(value);
    if (!calls.length) continue;
    const detail = node('details');
    detail.append(node('summary', label + '调用'), node('pre', textValue(calls)));
    root.append(detail);
  }
}

function callDetails(calls) {
  const values = entries(calls);
  if (!values.length) return null;
  const detail = node('details', undefined, 'call-list');
  detail.append(node('summary', 'Unit 调用 · ' + values.length));
  for (const call of values) {
    const row = node('div', undefined, 'call-row');
    if (typeof call === 'string') row.append(node('span', call));
    else {
      const title = call.unit?.name || call.unit_id || call.unit?.id || call.unit || call.name || call.gate || call.id || 'Unit 调用';
      row.append(node('strong', textValue(title)));
      if (call.status || call.physical) row.append(pill(call.status || call.physical));
      if (latency(call.latency_ms)) row.append(node('span', latency(call.latency_ms), 'card-meta'));
      row.append(node('pre', textValue(call)));
    }
    detail.append(row);
  }
  return detail;
}

function planDetails(task) {
  const plan = task.plan;
  if (!plan) return null;
  const detail = node('details', undefined, 'plan-details');
  detail.open = true;
  detail.append(node('summary', 'TaskPlan · ' + (plan.id || '本次方案')));
  const states = node('div', undefined, 'operation-states');
  if (plan.execution_status) states.append(pill(plan.execution_status));
  if (plan.semantic_status) states.append(pill(plan.semantic_status));
  detail.append(states);
  if (plan.binding_status) detail.append(node('p', statusLabel(plan.binding_status), 'card-meta'));
  if (typeof plan.candidate_count === 'number') detail.append(node('p', '上层方案候选：' + plan.candidate_count, 'card-meta'));
  const dataEdges = entries(plan.data_edges);
  const controlEdges = entries(plan.control_edges);
  const edges = node('div', undefined, 'plan-edges');
  edges.append(node('strong', '数据边 · ' + dataEdges.length));
  for (const edge of dataEdges) {
    const sourcePath = (edge.result_path || []).map(part => '[' + textValue(part) + ']').join('');
    const targetPath = (edge.argument_path || []).map(part => '[' + textValue(part) + ']').join('');
    edges.append(node('p', edge.source_step + sourcePath + ' → ' + edge.target_step + '.' + edge.argument + targetPath, 'edge-row'));
  }
  if (!dataEdges.length) edges.append(node('p', '本方案没有前项结果绑定。', 'card-meta'));
  edges.append(node('strong', '控制边 · ' + controlEdges.length));
  for (const edge of controlEdges) edges.append(node('p', edge.source_step + ' · ' + (edge.exit || 'RETURN') + ' → ' + edge.target_step, 'edge-row'));
  if (!controlEdges.length) edges.append(node('p', '当前只有单个入口或未形成后继控制边。', 'card-meta'));
  detail.append(edges);
  const results = plan.actual_results ?? plan.results ?? task.actual_results;
  if (results !== undefined && results !== null) detailValue(detail, '实际结果', results, true);
  detailValue(detail, '方案完整字段', plan);
  return detail;
}

function renderTasks() {
  const tasks = visibleTasks();
  const root = $('task-list');
  root.replaceChildren();
  $('task-count').textContent = tasks.length;
  if (!tasks.length) return empty(root, '尚无任务。发送消息后在这里观察真实调用。');
  for (const task of [...tasks].sort((a, b) => String(b.started_at || b.created_at || '').localeCompare(String(a.started_at || a.created_at || '')))) {
    const card = node('article', undefined, 'status-card');
    card.dataset.itemId = task.id;
    const head = node('div', undefined, 'card-heading');
    head.append(node('strong', '任务 ' + task.id), pill(task.status));
    card.append(head);
    if (task.phase) card.append(node('p', ({conversation: '对话模型', harness_execution: 'Harness 执行'})[task.phase] || task.phase, 'card-meta'));
    if (typeof task.model_calls === 'number') card.append(node('p', '模型调用：' + task.model_calls + ' 次', 'card-meta'));
    if (latency(task.latency_ms)) card.append(node('p', '任务时延：' + latency(task.latency_ms), 'card-meta'));
    if (task.error) card.append(node('p', textValue(task.error), 'card-note message-error'));
    const plan = planDetails(task);
    if (plan) card.append(plan);
    const detail = callDetails(task.calls);
    if (detail) card.append(detail);
    else card.append(node('p', '暂无 Unit 调用记录。', 'card-meta'));
    if (!inspectPage) card.append(inspectLink('tasks', {task: task.id}));
    root.append(card);
  }
}

function renderUnits() {
  const units = visibleUnits();
  const root = $('unit-list');
  root.replaceChildren();
  $('unit-count').textContent = units.length;
  if (!units.length) return empty(root, selectedSource() ? '这个来源当前没有接入能力。已摘除来源的历史结果仍在“来源与分析”页面。' : '能力池为空。分析 Harness 后，以实际接入结果显示 Unit。');
  root.append(node('p', '源码绑定、实际初始化和逐操作条件分别显示。', 'card-note'));
  for (const unit of units) {
    const card = node('article', undefined, 'unit-card');
    card.dataset.itemId = unit.id;
    const head = node('div', undefined, 'card-heading');
    head.append(node('strong', unit.name || unit.id), pill(unit.status));
    card.append(head);
    const sourceId = unit.harness_id || unit.source_id;
    const source = (state.harnesses || []).find(item => item.id === sourceId);
    card.append(node('p', [unit.mode || unit.kind, source?.name || sourceId].filter(Boolean).join(' · '), 'card-meta'));
    if (unit.description) card.append(node('p', unit.description, 'card-note'));
    if (unit.reason) card.append(node('p', textValue(unit.reason), 'card-note'));
    card.append(node('p', '初始化：' + statusLabel(unit.initialization), 'card-meta'));
    if (unit.operations) {
      const operationList = node('div', undefined, 'operation-list');
      for (const [name, operation] of Object.entries(unit.operations)) {
        const item = node('div', undefined, 'operation-row');
        item.append(node('span', ({export_entry: '独立入口', schedule: '原位排期', parallel: '并行', remote: '远程'})[name] || name),
          pill(operation?.status));
        operationList.append(item);
        if (operation?.formal_proof === false) operationList.append(node('p', '此项未完成完整语义证明。', 'card-meta'));
        if (operation?.reason) operationList.append(node('p', textValue(operation.reason), 'card-note'));
      }
      card.append(operationList);
      detailValue(card, '逐操作依据', unit.operations);
    }
    detailValue(card, '调用输入', unit.input_schema);
    detailValue(card, unit.result_schema && Object.keys(unit.result_schema).length ? '原结果端口' : '原结果端口 · 类型未解析', unit.result_schema);
    if (unit.result_type) card.append(node('p', '实际返回类型：' + unit.result_type, 'card-meta'));
    if (unit.construction) detailValue(card, '原生构造与绑定', unit.construction);
    else card.append(node('p', unit.kind === 'function' ? '构造：原函数入口。' : '构造说明：见入口绑定依据。', 'card-meta'));
    if (unit.native_input_schemas && Object.keys(unit.native_input_schemas).length) detailValue(card, '原对象输入端口', unit.native_input_schemas);
    detailValue(card, '缺失条件', unit.missing);
    detailValue(card, '入口范围', unit.entry_profile || unit.profile);
    if (!inspectPage) card.append(inspectLink('units', {harness: sourceId, unit: unit.id}));
    root.append(card);
  }
}

function renderAnalyses() {
  const harnesses = (state.harnesses || []).filter(source => !selectedSource() || source.id === selectedSource());
  const analyses = state.analyses || [];
  const root = $('analysis-list');
  root.replaceChildren();
  $('analysis-count').textContent = analyses.filter(job => harnesses.some(source => source.id === job.harness_id)).length;
  if (!harnesses.length) return empty(root, '配置 Harness 地址后可开始分析。');
  for (const harness of harnesses) {
    const history = analyses.filter(item => item.harness_id === harness.id);
    const jobs = inspectPage ? history.length ? history : [null] :
      [analyses.find(item => item.id === harness.analysis_id) || history[0] || null];
    for (const job of jobs) {
    const card = node('article', undefined, 'status-card');
    card.dataset.itemId = harness.id + ':' + (job?.id || 'source');
    const head = node('div', undefined, 'card-heading');
    head.append(node('strong', harness.name || harness.address), pill(harness.status === 'DETACHED' ? 'DETACHED' : job?.status || harness.status));
    card.append(head, node('p', harness.address, 'card-meta'));
    const files = entryCount(harness.files);
    if (files !== null) card.append(node('p', files + ' 份可读文件' + (typeof harness.inventory_count === 'number' ? ' · 仓库共 ' + harness.inventory_count + ' 项文件' : ''), 'card-meta'));
    if (job?.rounds !== undefined) card.append(node('p', '分析模型响应 ' + job.rounds + ' 轮', 'card-meta'));
    if (inspectPage) {
      card.append(node('p', '源码版本：' + (harness.revision || '本地内容版本'), 'card-meta'));
      if (job) {
        card.append(node('p', '分析 ' + job.id + ' · ' + statusLabel(job.status) +
          (job.id === harness.analysis_id ? ' · 当前记录' : ' · 历史记录'), 'card-meta'));
        if (job.started_at) card.append(node('p', '开始：' + job.started_at, 'card-meta'));
        if (job.refinement_of) card.append(node('p', '沿用分析上下文：' + job.refinement_of, 'card-meta'));
        detailValue(card, '实际模型用量', job.usage);
      }
    }
    if (harness.status === 'DETACHED') card.append(node('p', '来源已退出当前能力池。源码和分析历史保留，可重新接入。', 'card-note'));
    const error = job?.error || harness.error;
    if (error) card.append(node('p', textValue(error), 'card-note message-error'));
    const boundaries = job?.submitted?.candidate?.boundaries || job?.boundaries || [];
    if (boundaries.length) {
      const detail = node('details');
      detail.append(node('summary', '拆分结果 · ' + boundaries.length + ' 个边界'), node('pre', textValue(boundaries)));
      card.append(detail);
    }
    const entryList = job?.submitted?.candidate?.entries || [];
    if (entryList.length) {
      const detail = node('details');
      detail.append(node('summary', '接入入口 · ' + entryList.length), node('pre', textValue(entryList)));
      card.append(detail);
    }
    const entryReport = job?.submitted?.report?.entry_bindings;
    if (entryReport) detailValue(card, '入口绑定与缺失判断', entryReport);
    if (inspectPage && job?.submitted?.report) detailValue(card, '边界检查完整报告', job.submitted.report);
    const actions = node('div', undefined, 'harness-actions');
    if (!inspectPage) actions.append(inspectLink('analyses', {harness: harness.id}));
    actions.append(sourceButton(harness));
    card.append(actions);
    root.append(card);
    }
  }
  if (state.rules?.theory_pending) root.append(node('p', state.rules.theory_pending === true ?
    '分析准则待确认。' : textValue(state.rules.theory_pending), 'theory-note'));
}

function hydrateModels() {
  for (const role of ['chat', 'analysis']) {
    const form = $(role + '-model-form');
    const profile = state.models?.[role];
    if (!form.dataset.hydrated) {
      form.elements.base_url.value = profile?.base_url || '';
      form.elements.model.value = profile?.model || '';
      form.elements.max_output_tokens.value = profile?.max_output_tokens || '';
      form.dataset.hydrated = 'true';
    }
    form.elements.api_key.placeholder = profile?.key_present ? '同一接口留空保持；更换地址填写新 Key' :
      role === 'chat' ? '本地接口可留空' : '填写云端 API Key';
    const readiness = $(role + '-model-readiness');
    readiness.textContent = profile?.credential_error || (isReady(profile) ?
      '已配置 · ' + (profile.key_present ? '密钥已配置' : '未配置 Key') :
      profile?.configured ? '地址与模型已保存，请填写云端 API Key。' : '尚未配置');
    readiness.classList.toggle('message-error', Boolean(profile?.credential_error));
  }
}

function renderHarnesses() {
  const root = $('harness-list');
  root.replaceChildren();
  if (!(state.harnesses || []).length) return empty(root, '尚未配置 Harness 来源。');
  for (const harness of state.harnesses) {
    const card = node('article', undefined, 'harness-card');
    card.dataset.itemId = harness.id;
    const head = node('div', undefined, 'card-heading');
    head.append(node('strong', harness.name || harness.address), pill(harness.status));
    card.append(head, node('p', harness.address, 'card-note'));
    if (harness.error) card.append(node('p', textValue(harness.error), 'card-note message-error'));
    const job = (state.analyses || []).find(item => item.id === harness.analysis_id);
    const detached = harness.status === 'DETACHED';
    const busy = analysisSubmitting.has(harness.id) || ['RUNNING', 'QUEUED', 'ANALYZING'].includes(String(job?.status || harness.status).toUpperCase());
    const action = node('button', busy ? '分析中…' : '分析 Harness', 'secondary-button');
    action.type = 'button';
    action.disabled = detached || busy || sourceUpdating.has(harness.id) || !isReady(state.models?.analysis);
    action.onclick = async () => {
      analysisSubmitting.add(harness.id);
      action.disabled = true;
      action.textContent = '正在提交…';
      try {
        await api('/api/analyze', {harness_id: harness.id});
        showNotice('Harness 分析已开始，来源与分析状态会持续更新。');
      } catch (error) { showNotice(error.message); }
      finally { analysisSubmitting.delete(harness.id); await refresh(); }
    };
    const actions = node('div', undefined, 'harness-actions');
    actions.append(action);
    actions.append(inspectLink('analyses', {harness: harness.id}), sourceButton(harness));
    if (detached) card.append(node('p', '已摘除；重新接入会使用已有分析，不重复调用分析模型。', 'card-note'));
    if (!isReady(state.models?.analysis)) actions.append(node('span', '先配置分析模型', 'form-note'));
    card.append(actions);
    root.append(card);
  }
}

async function saveModel(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const status = form.querySelector('.form-status');
  const body = {role: form.dataset.role, base_url: form.elements.base_url.value.trim(), model: form.elements.model.value.trim()};
  if (form.elements.api_key.value.trim()) body.api_key = form.elements.api_key.value.trim();
  body.max_output_tokens = form.elements.max_output_tokens.value ? Number(form.elements.max_output_tokens.value) : null;
  button.disabled = true;
  status.textContent = '正在保存…';
  try {
    await api('/api/models', body);
    form.elements.api_key.value = '';
    status.textContent = '已保存';
    await refresh();
  } catch (error) { status.textContent = error.message; }
  finally { button.disabled = false; }
}

async function addHarness(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const body = {address: form.elements.address.value.trim()};
  for (const key of ['revision', 'entry']) if (form.elements[key].value.trim()) body[key] = form.elements[key].value.trim();
  const status = $('harness-form-status');
  $('add-harness').disabled = true;
  status.textContent = '正在接入…';
  try {
    if (form.elements.resources.value.trim()) {
      body.resources = JSON.parse(form.elements.resources.value);
      if (!Array.isArray(body.resources)) throw new Error('资源配置需要是 JSON 列表');
      if (body.resources.some(item => typeof item !== 'string' && (!item || typeof item !== 'object' || Array.isArray(item) || typeof item.path !== 'string'))) {
        throw new Error('每项资源需要路径字符串，或包含 path 的对象');
      }
    }
    if (form.elements.configuration.value.trim()) {
      const configuration = JSON.parse(form.elements.configuration.value);
      const object = value => value && typeof value === 'object' && !Array.isArray(value);
      if (!object(configuration) || Object.keys(configuration).some(key => key !== 'entries') ||
          (configuration.entries !== undefined && !object(configuration.entries))) {
        throw new Error('初始化配置需要 {"entries":{"入口ID":{...}}} 对象');
      }
      const rejectApiKey = value => {
        if (Array.isArray(value)) { value.forEach(rejectApiKey); return; }
        if (!object(value)) return;
        for (const [key, item] of Object.entries(value)) {
          if (key.replace(/[_\-\s]/g, '').toLowerCase() === 'apikey') throw new Error('初始化配置不接受 API Key，请使用模型配置或原项目环境变量');
          rejectApiKey(item);
        }
      };
      for (const entry of Object.values(configuration.entries || {})) {
        if (!object(entry) || Object.keys(entry).some(key => !['fixed_arguments', 'constructor_arguments', 'initializers', 'binding_arguments'].includes(key)) ||
            Object.values(entry).some(value => !object(value)) ||
            (entry.initializers && Object.values(entry.initializers).some(value => !object(value)))) {
          throw new Error('入口配置需要参数对象；initializers 按原初始化方法名填写参数对象');
        }
      }
      rejectApiKey(configuration);
      body.configuration = configuration;
    }
    await api('/api/harnesses', body);
    form.reset();
    status.textContent = '已接入，可开始分析';
    await refresh();
  } catch (error) { status.textContent = error.message; }
  finally { $('add-harness').disabled = false; }
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    state = await api('/api/state');
    connected = true;
    $('connection').textContent = '已连接';
    $('version').textContent = state.version ? '核心 ' + state.version : '';
    document.querySelector('.connection-dot').classList.remove('disconnected');
    hydrateModels();
    updateInspectPage();
    renderSection('profile', state.entry_profile, renderProfile, ['entry-profile-summary', 'config-entry-profile']);
    synchronizeTask();
    renderSection('scheduler', state.scheduler, renderScheduler, ['scheduler-summary']);
    renderSection('tasks', visibleTasks(), renderTasks, ['task-list']);
    renderSection('units', [visibleUnits(), state.harnesses, selectedSource()], renderUnits, ['unit-list']);
    renderSection('analyses', [state.analyses, state.harnesses, selectedSource(), [...sourceUpdating]], renderAnalyses, ['analysis-list']);
    renderSection('harnesses', [state.harnesses, state.analyses, state.models?.analysis, [...analysisSubmitting], [...sourceUpdating]], renderHarnesses, ['harness-list']);
  } catch (error) {
    connected = false;
    $('connection').textContent = '连接中断';
    document.querySelector('.connection-dot').classList.add('disconnected');
    showNotice(error.message);
  } finally {
    refreshing = false;
    updateComposer();
  }
}

function resizeComposer() {
  const input = $('chat-input');
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 220) + 'px';
  updateComposer();
}

function observerOpen(open) {
  document.body.classList.toggle('observer-open', open);
  $('drawer-shade').hidden = !open;
  $('open-observer').setAttribute('aria-expanded', String(open));
}

$('chat-form').onsubmit = sendMessage;
$('chat-input').oninput = resizeComposer;
$('chat-input').onkeydown = event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!$('send-message').disabled) $('chat-form').requestSubmit();
  }
};
$('chat-model-form').onsubmit = saveModel;
$('analysis-model-form').onsubmit = saveModel;
$('harness-form').onsubmit = addHarness;
$('open-configuration').onclick = () => $('configuration').showModal();
$('close-configuration').onclick = () => $('configuration').close();
$('open-observer').onclick = () => observerOpen(!document.body.classList.contains('observer-open'));
$('close-observer').onclick = () => observerOpen(false);
$('drawer-shade').onclick = () => observerOpen(false);
$('inspect-source').onchange = async event => {
  const query = new URLSearchParams({view: inspectView});
  if (event.target.value) query.set('harness', event.target.value);
  inspectQuery = query;
  history.replaceState(null, '', '/inspect?' + query);
  renderedSections.clear();
  document.querySelector('.observer-scroll').scrollTop = 0;
  await refresh();
};
document.addEventListener('keydown', event => {
  if (event.key === 'Escape') observerOpen(false);
});
refresh();
setInterval(refresh, 1500);
