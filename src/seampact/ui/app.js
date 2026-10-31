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
let activeChatId = null;
let chatLegacy = false;
let chatRestoring = false;
let chatLoadVersion = 0;
let historySearchVersion = 0;
let historySearchTimer;
const preparationSummaries = new Map();
const preparationErrors = new Map();
const analysisSubmitting = new Set();
const sourceUpdating = new Set();
const revisionDetails = new Map();
const revisionDrafts = new Map();
const revisionFeedbackDrafts = new Map();
const revisionSelected = new Map();
const revisionCreation = new Map();
const revisionBusy = new Set();
const revisionErrors = new Map();
const revisionVersions = new Map();
const deploymentDrafts = new Map();
const deploymentSubmitting = new Set();
const deploymentPreparation = new Map();
let inspectPage = location.pathname === '/inspect';
let configurationPage = location.pathname === '/configuration';
const configNames = {models: '模型角色', services: '服务连接', weights: '权重资源', harnesses: 'Harness 接入', research: '研究运行', scope: '入口范围'};
let configSection = new URLSearchParams(location.search).get('section') || 'models';
if (!configNames[configSection]) configSection = 'models';
let configurationReturnUrl = '/';
const configurationScroll = new Map();
const observerScroll = new Map();
let activePageUrl = location.pathname + location.search;
let inspectQuery = new URLSearchParams(location.search);
let inspectView = inspectQuery.get('view') || 'analyses';
const viewNames = {analyses: '来源与分析', units: '能力池', tasks: '任务详情', scheduler: '调度器', deployments: '运行组与部署'};
if (!viewNames[inspectView]) inspectView = 'analyses';
const renderedSections = new Map();
const detailStates = new Map();
let researchHydrated = false;
let researchDraft = {};
let researchDirty = false;
const researchSelected = new Set();
const researchDetails = new Map();
const researchDetailVersions = new Map();
const researchLoading = new Set();
const learnerNames = {double_dqn: 'Double DQN', ppo: 'PPO', discrete_sac: '离散 SAC', required_only: '仅必需工作', all_applicable: '全部适用工作'};
const researchModeNames = {train: '新训练', freeze: '冻结评价', warmstart: '继续训练', rejudge: '条件重评'};
const terminalStatuses = new Set(['COMPLETED', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED', 'BLOCKED']);
const statusNames = {
  RUNNING: '运行中', QUEUED: '排队中', WAITING: '等待中', COMPLETED: '已完成',
  SUCCEEDED: '已完成', FAILED: '失败', CANCELLED: '已取消', CANCELLING: '正在取消', INTERRUPTED: '已中断',
  READY: '源码已绑定', AVAILABLE: '可用', BLOCKED: '待接入', PENDING: '待确认',
  THEORY_PENDING: '理论待确认', IDLE: '空闲', ANALYZING: '分析中', ANALYZED: '已分析',
  REGISTERED: '已登记', CONNECTED: '已接入', MISSING: '待接入', NOT_CONFIGURED: '未配置', ENABLED: '已使能',
  AUTHORIZED: '已派发', RESERVED: '已占槽', UNKNOWN: '状态未知',
  STARTED: '执行中', RETIRED: '已结束', BUILT_DRAFT: '适配已生成',
  SOURCE_BOUND: '源码绑定已核对', ADMITTED: '操作条件成立', NOT_RUN: '尚未运行', BOUND: '原生绑定已执行',
  UNRESOLVED: '语义条件未决', RETURNED: '正常返回', STOPPED_AT_NON_SUCCESS_EXIT: '后继已停止',
  CHECKED_WITH_RUNTIME_VALUE_CONDITIONS: '结构已核对 · 按实际值检查输入', SOURCE_CHECKED: '源码已核对',
  DETACHED: '已摘除', SUPERSEDED: '已替换', REMOTE_CHECKED: '远程条件已核对',
  LOCAL_ONLY: '保留本地', EXPORTED: '运行包已导出', LOCAL: '本地进程', REMOTE: '独立运行进程',
  HOST_CHECKED: '宿主条件已核对', ARTIFACT_READY: '构建工件已生成', BUILT: '已构建', PUBLISHED: '已发布',
  NOT_REQUIRED: '无需发布', OPERATOR_REPORTED: '操作者报告', BACKEND_UNAVAILABLE: '后端不可用',
  ENVIRONMENT_DEFERRED: '环境条件由宿主核对', DRAFT: '修订草稿', CHECKED: '已完整检查', INSTALLED: '已安装'
};

function inspectLink(view, parameters = {}) {
  const query = new URLSearchParams({view, ...parameters});
  const link = node('a', '查看完整详情', 'detail-link');
  link.href = '/inspect?' + query;
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
  const environmentFocus = focus?.dataset.deploymentField;
  const selection = environmentFocus && ['INPUT', 'TEXTAREA'].includes(focus.tagName) ?
    {start: focus.selectionStart, end: focus.selectionEnd} : null;
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
  if (environmentFocus) {
    const replacement = roots.flatMap(id => [...$(id).querySelectorAll('[data-deployment-field]')])
      .find(input => input.dataset.deploymentField === environmentFocus);
    if (replacement && !replacement.disabled) {
      replacement.focus({preventScroll: true});
      if (selection?.start !== null && selection?.start !== undefined && replacement.setSelectionRange) {
        replacement.setSelectionRange(selection.start, selection.end);
      }
    }
  }
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

function cancelAnalysisButton(harness, job) {
  if (!job || !['RUNNING', 'CANCELLING'].includes(job.status)) return null;
  const button = node('button', job.status === 'CANCELLING' ? '正在取消…' : '取消本次分析', 'secondary-button');
  button.type = 'button';
  button.disabled = sourceUpdating.has(harness.id) || job.status === 'CANCELLING';
  button.onclick = async () => {
    if (sourceUpdating.has(harness.id)) return;
    sourceUpdating.add(harness.id); button.disabled = true;
    try {
      const result = await api('/api/analysis/cancel', {analysis_id: job.id});
      showNotice(result.status === 'CANCELLED' ? '本次分析已取消，来源与已有能力保留。' :
        result.status === 'CANCELLING' ? '已请求取消，本次分析结束后状态会更新；来源与已有能力保留。' :
        '本次分析已结束：' + statusLabel(result.status));
    } catch (error) { showNotice(error.message); }
    finally { sourceUpdating.delete(harness.id); await refresh(); }
  };
  return button;
}

function deleteSourceButton(harness) {
  const button = node('button', '删除来源记录', 'secondary-button danger-button');
  button.type = 'button';
  button.disabled = sourceUpdating.has(harness.id) || analysisSubmitting.has(harness.id);
  button.onclick = async () => {
    if (sourceUpdating.has(harness.id)) return;
    if (!window.confirm('删除“' + (harness.name || harness.address) + '”的来源记录？\n\n正在进行的分析会取消，相关能力会退出能力池，来源与分析将从列表移除，无法直接恢复。平台克隆到本地的仓库会删除；登记在这些仓库里的权重一并删除。放在独立目录的权重、你填写的原本地目录、冻结快照和已有任务依据保留。正在执行的任务请先结束。')) return;
    sourceUpdating.add(harness.id); button.disabled = true;
    try {
      const result = await api('/api/harnesses/delete', {harness_id: harness.id});
      const weights = (result.removed_weights || []).length ? '登记在该仓库里的权重已一并删除。' : '';
      showNotice(result.source_files === 'REMOVED'
        ? '来源记录已删除，平台保存的本地仓库已清理。' + weights + '独立目录中的权重、冻结快照与已有任务历史保留。'
        : result.source_files === 'PARTIAL'
        ? '来源记录已删除。仍被其他来源使用的平台仓库保留，其余已清理。' + weights
        : '来源记录已删除。这次来源使用的是原本地目录，没有删除该目录。独立目录中的权重、冻结快照与已有任务历史保留。');
      if (selectedSource() === harness.id) {
        inspectQuery.delete('harness');
        history.replaceState({}, '', '/inspect?' + inspectQuery);
        activePageUrl = location.pathname + location.search;
      }
    } catch (error) { showNotice(error.message); }
    finally { sourceUpdating.delete(harness.id); await refresh(); }
  };
  return button;
}

function updateInspectPage() {
  document.body.classList.toggle('inspect-page', inspectPage);
  document.body.classList.toggle('configuration-page', configurationPage);
  $('configuration').hidden = !configurationPage;
  document.querySelector('.chat-shell').setAttribute('aria-hidden', String(inspectPage || configurationPage));
  document.querySelector('.rail-caption').textContent = '异构 Harness · ' + (configurationPage ? '配置' : inspectPage ? '详情' : '对话');
  document.title = 'SeamPact · ' + (configurationPage ? '配置 · ' + configNames[configSection] : inspectPage ? viewNames[inspectView] : '对话');
  $('open-configuration').classList.toggle('selected', configurationPage);
  document.querySelector('.chat-navigation').classList.toggle('selected', !inspectPage && !configurationPage);
  for (const section of document.querySelectorAll('[data-config-section]')) section.hidden = section.dataset.configSection !== configSection;
  for (const link of document.querySelectorAll('[data-config-link]')) {
    const selected = link.dataset.configLink === configSection;
    link.classList.toggle('selected', selected);
    if (selected) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current');
  }
  $('close-configuration').href = configurationReturnUrl;
  $('close-configuration').textContent = configurationReturnUrl.startsWith('/inspect') ? '返回运行详情' : '返回对话';
  for (const section of document.querySelectorAll('.observer-scroll > .panel-section')) section.hidden = inspectPage && section.dataset.view !== inspectView;
  for (const link of document.querySelectorAll('.rail-actions a[href^="/inspect"]')) {
    const view = new URL(link.href).searchParams.get('view');
    link.classList.toggle('selected', inspectPage && view === inspectView);
  }
  $('observer-title').textContent = inspectPage ? viewNames[inspectView] : '运行状态';
  $('inspect-description').hidden = !inspectPage;
  $('inspect-filter').hidden = !inspectPage || inspectView === 'scheduler';
  if (!inspectPage) return;
  for (const link of document.querySelectorAll('.rail-actions a[href^="/inspect"]')) {
    const view = new URL(link.href).searchParams.get('view');
    const query = new URLSearchParams({view});
    if (selectedSource() && view !== 'scheduler') query.set('harness', selectedSource());
    link.href = '/inspect?' + query;
    link.removeAttribute('target');
    link.classList.toggle('selected', view === inspectView);
  }
  const options = (state?.harnesses || []).map(source => [source.id, source.name || source.address, source.status]);
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

function navigatePage(href, push = true) {
  const target = new URL(href, location.href);
  const previous = activePageUrl;
  if (configurationPage) configurationScroll.set(configSection, document.querySelector('.config-body').scrollTop);
  if (inspectPage) observerScroll.set(previous, document.querySelector('.observer-scroll').scrollTop);
  if (target.pathname === '/configuration' && !configurationPage && push) configurationReturnUrl = previous;
  if (push) history.pushState(null, '', target.pathname + target.search + target.hash);
  activePageUrl = target.pathname + target.search;
  inspectPage = target.pathname === '/inspect';
  configurationPage = target.pathname === '/configuration';
  if (inspectPage || configurationPage) { chatLoadVersion += 1; chatRestoring = false; }
  inspectQuery = new URLSearchParams(target.search);
  inspectView = viewNames[inspectQuery.get('view')] ? inspectQuery.get('view') : 'analyses';
  configSection = configNames[inspectQuery.get('section')] ? inspectQuery.get('section') : 'models';
  observerOpen(false);
  updateInspectPage();
  document.querySelector('.config-body').scrollTop = configurationScroll.get(configSection) || 0;
  if (inspectPage) document.querySelector('.observer-scroll').scrollTop = observerScroll.get(target.pathname + target.search) || 0;
  renderedSections.clear();
  if (!inspectPage && !configurationPage) restoreChatFromLocation();
  if (state) refresh();
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
  const notice = $(configurationPage ? 'configuration-notice' : inspectPage ? 'inspect-notice' : 'notice');
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
    root.append(node('p', [profile.interpreter + '（平台本地基线；运行组另选目标 Python）', profile.host === 'local-native-process' ? '本地原生进程' : profile.host,
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

function renderInline(parent, text) {
  const pattern = /(`+)([^`\n]+?)\1|\*\*([^*\n]+)\*\*|\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g;
  let last = 0;
  for (let match = pattern.exec(text); match; match = pattern.exec(text)) {
    if (match.index > last) parent.append(document.createTextNode(text.slice(last, match.index)));
    if (match[2] !== undefined) parent.append(node('code', match[2]));
    else if (match[3] !== undefined) parent.append(node('strong', match[3]));
    else {
      const link = node('a', match[4]);
      link.href = match[5];
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      parent.append(link);
    }
    last = pattern.lastIndex;
  }
  if (last < text.length) parent.append(document.createTextNode(text.slice(last)));
}

function appendMarkdownBlocks(parent, text) {
  const lines = text.replace(/\n+$/, '').split('\n');
  let paragraph = [];
  let list = null;
  const flushParagraph = () => {
    if (!paragraph.length) return;
    const block = node('p');
    paragraph.forEach((line, index) => {
      if (index) block.append(node('br'));
      renderInline(block, line);
    });
    parent.append(block);
    paragraph = [];
  };
  for (const line of lines) {
    if (!line.trim()) {
      flushParagraph();
      list = null;
      continue;
    }
    const heading = /^(#{1,3})\s+(.+)$/.exec(line);
    if (heading) {
      flushParagraph();
      list = null;
      const block = node('h' + (heading[1].length + 2));
      renderInline(block, heading[2]);
      parent.append(block);
      continue;
    }
    const item = /^(\s*)([-+*]|\d+[.)])\s+(.+)$/.exec(line);
    if (item && item[1].length < 4) {
      flushParagraph();
      const ordered = /\d/.test(item[2]);
      if (!list || list.ordered !== ordered) {
        list = {ordered, element: node(ordered ? 'ol' : 'ul')};
        parent.append(list.element);
      }
      const entry = node('li');
      renderInline(entry, item[3]);
      list.element.append(entry);
      continue;
    }
    list = null;
    paragraph.push(line);
  }
  flushParagraph();
}

function renderMarkdown(body, text) {
  body.classList.add('markdown');
  body.replaceChildren();
  const source = String(text ?? '').replace(/\r\n/g, '\n');
  if (!source) return;
  const fence = /```([^\n`]*)\n([\s\S]*?)```/g;
  const blocks = [];
  let last = 0;
  for (let match = fence.exec(source); match; match = fence.exec(source)) {
    if (match.index > last) blocks.push({kind: 'text', text: source.slice(last, match.index)});
    blocks.push({kind: 'code', language: match[1].trim(), text: match[2].replace(/\n$/, '')});
    last = fence.lastIndex;
  }
  const rest = source.slice(last);
  const open = /```([^\n`]*)\n([\s\S]*)$/.exec(rest);
  if (open) {
    if (open.index) blocks.push({kind: 'text', text: rest.slice(0, open.index)});
    blocks.push({kind: 'code', language: open[1].trim(), text: open[2]});
  } else if (rest) blocks.push({kind: 'text', text: rest});
  for (const block of blocks) {
    if (block.kind === 'code') {
      const pre = node('pre');
      const code = node('code', block.text);
      if (/^[\w.+-]+$/.test(block.language)) code.dataset.language = block.language;
      pre.append(code);
      body.append(pre);
    } else appendMarkdownBlocks(body, block.text);
  }
}

function setMessageBody(body, text, markdown) {
  if (!markdown) {
    body.classList.remove('markdown');
    body.textContent = text ?? '';
    return;
  }
  renderMarkdown(body, text);
}

function appendMessage(role, text, markdown = role === 'assistant') {
  const welcome = $('conversation').querySelector('.welcome');
  if (welcome) welcome.remove();
  const item = node('article', undefined, 'chat-message ' + role);
  const content = node('div', undefined, 'message-content');
  const body = node('div', undefined, 'message-body');
  setMessageBody(body, text, markdown);
  const meta = node('div', undefined, 'message-meta');
  content.append(node('div', role === 'user' ? '你' : 'SeamPact', 'message-role'), body, meta);
  item.append(node('div', role === 'user' ? '你' : 'S', 'message-avatar'), content);
  $('conversation').append(item);
  item.scrollIntoView({block: 'end', behavior: 'auto'});
  return {item, body, meta};
}

function setChatAddress(identity, push = false) {
  const url = identity ? '/?chat=' + encodeURIComponent(identity) : '/';
  history[push ? 'pushState' : 'replaceState'](null, '', url);
  activePageUrl = url;
  for (const link of document.querySelectorAll('.chat-navigation')) link.href = url;
}

function resetChat(push = true) {
  if (pendingTask || chatRestoring) return;
  chatLoadVersion += 1;
  activeChatId = null; chatLegacy = false; messages.length = 0;
  $('conversation').replaceChildren();
  const welcome = node('div', undefined, 'welcome');
  welcome.append(node('h2', '有什么想一起完成的？'), node('p', '发送消息开始新对话；历史会话仍保留。'));
  $('conversation').append(welcome);
  $('chat-title').textContent = '新对话';
  $('chat-input').value = ''; $('scene-task-selector').value = '';
  setChatAddress(null, push); showNotice(''); resizeComposer();
  if (!$('chat-history').hidden) loadChatHistory();
}

async function loadChatHistory() {
  const version = ++historySearchVersion;
  const root = $('chat-history-list');
  try {
    const result = await api('/api/chats?q=' + encodeURIComponent($('chat-history-search').value.trim()));
    if (version !== historySearchVersion) return;
    root.replaceChildren();
    for (const chat of result.conversations || []) {
      const button = node('button', undefined, 'chat-history-item'); button.type = 'button';
      button.setAttribute('aria-current', String(chat.id === activeChatId));
      const time = chat.updated_at ? new Date(chat.updated_at).toLocaleString('zh-CN') : '时间未记录';
      button.append(node('strong', chat.title), node('span', time + ' · ' + statusLabel(chat.status) +
        ' · ' + chat.turns + ' 轮' + (chat.legacy ? ' · 仅保存回答' : '')));
      button.disabled = Boolean(pendingTask) || chatRestoring;
      button.onclick = () => loadConversation(chat.id, true);
      const row = node('div', undefined, 'chat-history-row');
      const remove = node('button', '删除', 'icon-button chat-history-delete'); remove.type = 'button';
      remove.setAttribute('aria-label', '删除会话：' + chat.title);
      remove.disabled = Boolean(pendingTask) || chatRestoring || chat.status === 'RUNNING';
      remove.onclick = () => deleteConversation(chat.id, remove);
      row.append(button, remove); root.append(row);
    }
    if (!root.childElementCount) empty(root, '没有匹配的历史会话。');
  } catch (error) { if (version === historySearchVersion) root.replaceChildren(node('p', error.message, 'message-error')); }
}

async function deleteConversation(identity, button) {
  button.disabled = true;
  const feedback = $('chat-history-feedback');
  try {
    await api('/api/chats/delete', {conversation_id: identity});
    if (identity === activeChatId) resetChat(false);
    feedback.replaceChildren(node('span', '会话已从历史删除，任务执行记录保留。'));
    const undo = node('button', '撤销删除', 'icon-button'); undo.type = 'button';
    undo.onclick = async () => {
      undo.disabled = true;
      try {
        await api('/api/chats/restore', {conversation_id: identity});
        feedback.replaceChildren(node('span', '会话已恢复。')); await loadChatHistory();
      } catch (error) { feedback.replaceChildren(node('span', error.message, 'message-error')); }
    };
    feedback.append(undo); await loadChatHistory();
  } catch (error) {
    feedback.replaceChildren(node('span', error.message, 'message-error')); button.disabled = false;
  }
}

async function loadConversation(identity, push = false) {
  if (pendingTask || chatRestoring) return;
  const version = ++chatLoadVersion;
  chatRestoring = true; updateComposer();
  try {
    const chat = await api('/api/chats/' + encodeURIComponent(identity));
    if (version !== chatLoadVersion) return;
    activeChatId = chat.id; chatLegacy = chat.legacy; messages.splice(0, messages.length, ...chat.messages);
    $('conversation').replaceChildren();
    for (const message of messages) appendMessage(message.role, responseText(message.content));
    const first = messages.find(message => message.role === 'user');
    $('chat-title').textContent = chat.legacy ? '旧对话记录' : first ? responseText(first.content).slice(0, 32) : '对话';
    $('chat-input').value = ''; $('scene-task-selector').value = '';
    const task = chat.task;
    if (task.status === 'RUNNING') {
      const output = appendMessage('assistant', '正在运行…', false); output.item.classList.add('message-pending');
      pendingTask = {id: task.id, output, sceneTaskId: null};
    } else if (!['COMPLETED', 'SUCCEEDED'].includes(task.status)) {
      const output = appendMessage('assistant', task.error || statusLabel(task.status), false); output.item.classList.add('message-error');
    }
    setChatAddress(chat.id, push);
    showNotice(chat.legacy ? '这条旧记录只保存了回答，原问题无法恢复。请新建对话继续。' : '');
    $('chat-history').hidden = true; $('toggle-chat-history').setAttribute('aria-expanded', 'false');
  } catch (error) { showNotice(error.message); }
  finally { if (version === chatLoadVersion) { chatRestoring = false; resizeComposer(); } }
}

function restoreChatFromLocation() {
  const identity = new URLSearchParams(location.search).get('chat');
  if (pendingTask && identity !== activeChatId) { setChatAddress(activeChatId); return; }
  if (identity && identity !== activeChatId) loadConversation(identity);
  else if (!identity && activeChatId && !pendingTask) resetChat(false);
}

function hydrateSceneTasks() {
  const selector = $('scene-task-selector');
  const active = researchState().active_checkpoint;
  const tasks = Array.isArray(active?.task_ids) ? active.task_ids : [];
  $('scene-task-label').hidden = tasks.length === 0;
  const signature = JSON.stringify([active?.run_id, active?.scene_id, tasks]);
  if (selector.dataset.options !== signature) {
    const selected = selector.dataset.binding === active?.run_id ? selector.value : '';
    selector.replaceChildren(node('option', '普通对话')); selector.firstChild.value = '';
    for (const identity of tasks) {
      const option = node('option', (active.scene_id || '已保存场景') + ' · ' + identity);
      option.value = String(identity); selector.append(option);
    }
    selector.value = tasks.some(identity => String(identity) === selected) ? selected : '';
    selector.dataset.binding = active?.run_id || '';
    selector.dataset.options = signature;
  }
  selector.disabled = Boolean(pendingTask);
}

function updateComposer() {
  const selectedSceneTask = $('scene-task-selector').value;
  const active = researchState().active_checkpoint;
  const ready = connected && (selectedSceneTask ? Boolean(active?.run_id) : isReady(state?.models?.chat));
  $('send-message').disabled = !ready || Boolean(pendingTask) || chatRestoring || (chatLegacy && !selectedSceneTask) || !$('chat-input').value.trim();
  $('new-chat').disabled = Boolean(pendingTask) || chatRestoring;
  $('scene-task-selector').disabled = Boolean(pendingTask);
  $('send-message').querySelector('.send-label').textContent = pendingTask ? '运行中' : selectedSceneTask ? '运行任务' : '发送';
  $('chat-input').placeholder = selectedSceneTask ? '如：运行所选任务（文字仅作为本次运行附言）' : '发送消息…';
  $('composer-hint').textContent = selectedSceneTask ? '执行已保存场景任务，不改写原任务；文字作为运行附言记录。' : 'Enter 发送 · Shift + Enter 换行';
  if (chatRestoring) $('composer-hint').textContent = '正在读取历史会话…';
  else if (chatLegacy && !selectedSceneTask) $('composer-hint').textContent = '此旧记录没有保存问题，请点击“新对话”。';
  $('chat-model-name').textContent = selectedSceneTask ? '冻结策略 · ' + (active?.scene_id || '已保存场景') :
    isReady(state?.models?.chat) ? state.models.chat.model : '对话模型未配置';
  $('chat-model-name').className = 'status-pill ' + ((selectedSceneTask ? active?.run_id : isReady(state?.models?.chat)) ? 'ready' : 'pending');
  const units = state?.units || [];
  const available = units.filter(unit => ['READY', 'AVAILABLE'].includes(String(unit.status).toUpperCase())).length;
  const hint = !connected ? '平台未连接。' : selectedSceneTask ?
    '冻结场景：' + (active?.scene_id || '—') + ' · 任务 ' + selectedSceneTask + ' · 来源运行 ' + (active?.run_id || '—') : !isReady(state?.models?.chat) ?
    '先在配置中填写对话模型接口。' : available ?
    '对话已就绪 · 能力池中有 ' + available + ' 个源码已绑定入口。' :
    '对话已就绪 · 当前没有可调用 Unit；Harness 状态见右侧。';
  $('chat-readiness').textContent = hint;
  if ($('welcome-context')) $('welcome-context').textContent = hint;
}

async function sendMessage(event) {
  event.preventDefault();
  const content = $('chat-input').value.trim();
  const sceneTaskId = $('scene-task-selector').value;
  const sceneRunId = researchState().active_checkpoint?.run_id;
  if (!content || pendingTask || chatRestoring || (chatLegacy && !sceneTaskId) || !connected ||
      (sceneTaskId ? !sceneRunId : !isReady(state?.models?.chat))) return;
  if (!sceneTaskId) messages.push({role: 'user', content});
  const user = appendMessage('user', content);
  if (sceneTaskId) user.meta.textContent = '运行附言 · 已保存任务 ' + sceneTaskId + ' · 不改写原任务';
  $('chat-input').value = '';
  resizeComposer();
  const output = appendMessage('assistant', '正在提交任务…', false);
  output.item.classList.add('message-pending');
  pendingTask = {id: null, output, sceneTaskId: sceneTaskId || null};
  updateComposer();
  showNotice('');
  try {
    const body = sceneTaskId ? {scene_task_id: sceneTaskId, scene_run_id: sceneRunId, messages: [{role: 'user', content}]} :
      {messages, ...(activeChatId ? {conversation_id: activeChatId} : {})};
    const result = await api('/api/chat', body);
    if (!result.task_id) throw new Error('平台未返回任务 ID');
    pendingTask.id = result.task_id;
    if (!sceneTaskId && result.conversation_id) {
      activeChatId = result.conversation_id; setChatAddress(activeChatId);
      $('chat-title').textContent = responseText(messages.find(message => message.role === 'user')?.content || '对话').slice(0, 32);
    }
    setMessageBody(output.body, '正在运行…', false);
    await refresh();
  } catch (error) {
    setMessageBody(output.body, error.message, false);
    output.item.classList.remove('message-pending');
    output.item.classList.add('message-error');
    pendingTask = null;
    if (!$('chat-history').hidden) loadChatHistory();
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
  setMessageBody(output.body, text || (task.error ? textValue(task.error) : statusLabel(task.status) + '…'), Boolean(text));
  const duration = latency(task.latency_ms);
  output.meta.textContent = [statusLabel(task.status), duration].filter(Boolean).join(' · ');
  if (terminalStatuses.has(status)) {
    output.item.classList.remove('message-pending');
    if (['COMPLETED', 'SUCCEEDED'].includes(status)) {
      if (text) {
        if (!pendingTask.sceneTaskId) messages.push({role: 'assistant', content: text});
      } else setMessageBody(output.body, '任务已完成，平台未返回对话内容。', false);
    } else {
      output.item.classList.add('message-error');
      if (text && task.error) output.meta.textContent += ' · ' + textValue(task.error);
    }
    pendingTask = null;
    updateComposer();
    if (!$('chat-history').hidden) loadChatHistory();
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
    root.append(node('p', '历史表格 Q 状态仅供旧实现追溯。新的研究运行使用共享候选评分，上层选择获准干预，下层固定 FIFO；单候选执行不能说明学习收益。', 'card-note'));
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
    const policy = unit.scheduling;
    card.append(node('p', policy?.visibility === 'transparent' ? '透明能力 · 平台按任务事件激活' :
      policy?.visibility === 'business' ? '业务可见 · 业务模型可申请' : '激活方式未声明 · 沿用业务可见入口', 'card-meta'));
    if (inspectPage && !['DETACHED', 'SUPERSEDED'].includes(unit.status)) {
      const label = node('label', '激活入口', 'unit-policy-control');
      const selector = node('select'); selector.setAttribute('aria-label', '能力激活入口 ' + (unit.name || unit.id));
      for (const [value, text] of [['business', '业务可见，由业务模型申请'], ['transparent', '透明能力，由平台事件调度']]) {
        const option = node('option', text); option.value = value; selector.append(option);
      }
      selector.value = policy?.visibility || 'business';
      selector.onchange = async () => {
        selector.disabled = true;
        try {
          await api('/api/units/policy', {unit_id: unit.id, visibility: selector.value,
            activation: selector.value === 'transparent' ? 'platform' : 'business'});
          await refresh();
        } catch (error) { showNotice(error.message); selector.value = policy?.visibility || 'business'; }
        finally { selector.disabled = false; }
      };
      label.append(selector); card.append(label);
    }
    card.append(node('p', '初始化：' + statusLabel(unit.initialization), 'card-meta'));
    if (unit.runtime_group) card.append(node('p', '执行位置：' + statusLabel(unit.execution) + ' · ' + unit.runtime_group, 'card-meta'), inspectLink('deployments', {harness: sourceId}));
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
    detailValue(card, '共同能力卡', unit.capability);
    if (inspectPage) {
      const editor = node('details'); editor.append(node('summary', '编辑能力卡'));
      const input = node('textarea'); input.rows = 12; input.value = JSON.stringify(unit.capability || {}, null, 2);
      input.setAttribute('aria-label', '共同能力卡 JSON');
      const save = node('button', '保存能力卡', 'secondary-button'); save.type = 'button';
      save.onclick = async () => {
        try { await api('/api/units/capability', {unit_id: unit.id, card: JSON.parse(input.value)});
          showNotice('能力卡已保存，下次运行冻结新表示。'); await refresh(); }
        catch (error) { showNotice(error.message); }
      };
      editor.append(input, save); card.append(editor);
    }
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

const instanceStateNames = {UNDEPLOYED: '未部署', STARTING: '启动中', READY: '就绪', DRAINING: '排空中',
  STOPPED: '已停止', FAILED: '失败', UNKNOWN: '结果未决'};
const operationNames = {SUPPORTED: '允许', UNSUPPORTED: '不支持', UNKNOWN: '未知 · 不授予'};

function environmentEditor(initial, key) {
  const environment = deploymentDrafts.get(key) || initial || {};
  const form = node('form', undefined, 'model-form runtime-environment-form');
  const fields = {};
  const pythonLabel = node('label', '目标 Python（与基础镜像内解释器一致）');
  const python = node('select'); python.required = true;
  for (const version of ['', '3.10', '3.11', '3.12', '3.13']) {
    const option = node('option', version ? 'Python ' + version : '选择目标 Python'); option.value = version; python.append(option);
  }
  python.value = environment.python || ''; fields.python = python; pythonLabel.append(python); form.append(pythonLabel);
  for (const [name, title, value] of [
    ['base_image', '基础镜像（可使用含所需 CUDA／系统依赖的镜像）', environment.base_image || (python.value ? 'python:' + python.value + '-slim' : '')],
    ['requirements', '要安装的冻结源码依赖清单（每行一份，按原库选择）', (environment.requirements || []).join('\n')],
    ['project', '安装原项目的路径（. 或源码子目录；留空不安装）', environment.project || ''],
    ['packages', '额外 pip 需求（每行一项）', (environment.packages || []).join('\n')],
    ['pip_extra_index_url', '额外包索引（不含凭据）', environment.pip_extra_index_url || '']]) {
    const label = node('label', title);
    const input = node(['packages', 'requirements'].includes(name) ? 'textarea' : 'input');
    if (input.tagName === 'TEXTAREA') input.rows = 3;
    input.value = value; fields[name] = input; label.append(input); form.append(label);
  }
  const read = () => {
    const lines = text => text.split('\n').map(item => item.trim()).filter(Boolean);
    return {python: fields.python.value, base_image: fields.base_image.value.trim(),
      requirements: lines(fields.requirements.value), project: fields.project.value.trim(),
      packages: lines(fields.packages.value), pip_extra_index_url: fields.pip_extra_index_url.value.trim()};
  };
  for (const [name, field] of Object.entries(fields)) {
    field.dataset.deploymentField = key + '/' + name;
    field.oninput = () => deploymentDrafts.set(key, read());
  }
  let previousPython = python.value;
  python.onchange = () => {
    if (!fields.base_image.value || fields.base_image.value === 'python:' + previousPython + '-slim') {
      fields.base_image.value = python.value ? 'python:' + python.value + '-slim' : '';
    }
    previousPython = python.value; deploymentDrafts.set(key, read());
  };
  const load = value => {
    for (const [name, field] of Object.entries(fields)) {
      field.value = ['requirements', 'packages'].includes(name) ? (value[name] || []).join('\n') : value[name] || '';
    }
    previousPython = python.value; deploymentDrafts.set(key, read());
  };
  return {form, fields, read, load};
}

function environmentEvidence(card, discovery) {
  if (!discovery) {
    card.append(node('p', '仓库环境建议尚未生成；可依据原库声明填写。', 'card-note')); return;
  }
  card.append(node('p', '仓库环境建议：' + (discovery.status || 'UNKNOWN') +
    (discovery.suggested?.python ? ' · Python ' + discovery.suggested.python : ' · Python 版本待确定'), 'card-meta'));
  if (discovery.missing?.length) detailValue(card, '尚未确定的环境条件', discovery.missing);
  if (discovery.conflicts?.length) detailValue(card, '仓库环境声明冲突', discovery.conflicts);
  detailValue(card, '冻结仓库环境依据', discovery.evidence || {python_declarations: discovery.python_declarations,
    requirements: discovery.requirements, projects: discovery.projects, base_images: discovery.base_images,
    conda_files: discovery.conda_files});
}

function sourceEnvironment(card, harness) {
  const discovery = harness.environment_discovery;
  const selected = harness.runtime_environment;
  const target = harness.target_runtime || {};
  const labels = {READY: '目标解释器已准备', PREPARING: '目标解释器准备中', FAILED: '目标解释器准备失败',
    BACKEND_UNAVAILABLE: '解释器构建后端不可用', NOT_PREPARED: '目标解释器未准备'};
  const targetLabel = selected ? (labels[target.status] || target.status || '目标解释器未准备') : '沿用平台本地环境';
  const detail = node('details', undefined, 'runtime-environment');
  detail.append(node('summary', '运行环境 · ' + (selected?.python ? '已选 Python ' + selected.python : '未选择独立环境') + ' · ' + targetLabel));
  environmentEvidence(detail, discovery);
  if (selected) detailValue(detail, '已保存的环境选择', selected);
  if (harness.environment_pending_recheck) detail.append(node('p', '环境选择已变化，已有能力待重新核对。先准备目标解释器，再沿用完整候选重新核对。', 'card-note message-error'));
  detail.append(node('p', selected ? targetLabel + '。此状态只说明源码核对解释器，原库依赖安装、权重和真实 worker 就绪分别记录。' :
    '当前分析沿用平台本地环境；原库需要独立 Python 时在此选择并准备。依赖、权重与真实调用状态分别记录。', 'card-note'));
  if (target.reason) detail.append(node('p', textValue(target.reason), 'card-note' + (target.status === 'FAILED' ? ' message-error' : '')));
  detailValue(detail, '目标解释器记录', target);
  const key = 'source/' + harness.id;
  const {form, fields, read, load} = environmentEditor(selected || discovery?.suggested, key);
  const job = (state.analyses || []).find(item => item.id === harness.analysis_id);
  const locked = (state.runtime_groups || []).some(group => group.source_id === harness.id && group.locked_by_run);
  const busy = harness.status === 'DETACHED' || analysisSubmitting.has(harness.id) || sourceUpdating.has(harness.id) ||
    ['RUNNING', 'QUEUED', 'CANCELLING'].includes(job?.status) || target.status === 'PREPARING' || locked || deploymentSubmitting.has(key) ||
    deploymentPreparation.get(harness.id)?.running;
  for (const field of Object.values(fields)) field.disabled = busy;
  form.append(node('p', '表单用于选择环境。保存不会修改原库源码或已有研究记录；准备解释器不安装原库。仓库建议不会自动执行 Dockerfile，也不会把多份依赖清单全部合并。', 'card-note'));
  const actions = node('div', undefined, 'harness-actions');
  if (discovery?.suggested) {
    const suggested = node('button', '填写仓库建议', 'secondary-button'); suggested.type = 'button'; suggested.disabled = busy;
    suggested.onclick = () => load(discovery.suggested); actions.append(suggested);
  }
  const hasCandidate = job?.status === 'COMPLETED' && Boolean(job.submitted?.candidate);
  for (const [mode, label] of [['save', '保存环境选择'], ['prepare', '保存并准备目标解释器'], ['recheck', '按此环境重新核对已有候选']]) {
    const button = node('button', label, 'secondary-button'); button.type = 'submit'; button.value = mode;
    button.disabled = busy || (mode === 'recheck' && !hasCandidate);
    if (locked) button.title = '研究运行正在使用该来源；结束后再修改环境';
    else if (mode === 'recheck' && !hasCandidate) button.title = '先形成分析器候选，再使用零模型调用重新核对';
    actions.append(button);
  }
  form.append(actions, node('p', '重新核对沿用分析器候选，不调用分析模型；不会重新生成入口或拆分边界。', 'card-note'));
  form.onsubmit = async event => {
    event.preventDefault();
    if (busy || deploymentSubmitting.has(key)) return;
    const mode = event.submitter?.value || 'save';
    deploymentSubmitting.add(key);
    for (const control of form.elements) control.disabled = true;
    try {
      await api('/api/harnesses/environment', {harness_id: harness.id, environment: read(),
        ...(mode === 'prepare' ? {prepare: true} : mode === 'recheck' ? {recheck: true} : {})});
      deploymentDrafts.delete(key);
      showNotice(mode === 'prepare' ? '环境选择已保存，目标解释器准备已提交；状态见运行环境。' :
        mode === 'recheck' ? '已提交已有候选的环境重新核对，不调用分析模型。' : '环境选择已保存。已有候选需重新核对，新镜像需重新生成。');
    } catch (error) { showNotice(error.message); }
    finally {
      deploymentSubmitting.delete(key); renderedSections.delete('harnesses'); renderedSections.delete('deployments'); await refresh();
    }
  };
  detail.append(form);
  if (hasCandidate) deploymentPreparationControls(detail, harness);
  card.append(detail);
}

function operationId() {
  return Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12);
}

function heldInstance(instance) {
  return ['UNDEPLOYED', 'STARTING', 'READY', 'DRAINING', 'UNKNOWN'].includes(instance.state) ||
    (instance.state === 'FAILED' && (instance.handle || instance.endpoint) && !instance.released);
}

async function prepareDeployment(sourceId, groupId, releaseUnknown) {
  if (deploymentPreparation.get(sourceId)?.running || sourceUpdating.has(sourceId) || analysisSubmitting.has(sourceId)) return;
  const progress = {running: true, phase: '检查已保存配置'};
  deploymentPreparation.set(sourceId, progress);
  sourceUpdating.add(sourceId);
  const phase = async text => {
    progress.phase = text;
    renderedSections.delete('harnesses'); renderedSections.delete('deployments');
    await refresh();
  };
  let environment;
  const read = async () => {
    const snapshot = await api('/api/state');
    const source = snapshot.harnesses.find(item => item.id === sourceId);
    if (!source || source.status === 'DETACHED') throw new Error('来源不存在或已摘除');
    if (!source.runtime_environment) throw new Error('先在来源运行环境保存 Python、基础镜像和原依赖选择');
    if (environment && JSON.stringify(source.runtime_environment) !== JSON.stringify(environment))
      throw new Error('运行环境已改变，流程已停止；核对配置后重新点击');
    const groups = snapshot.runtime_groups.filter(item => item.source_id === sourceId);
    if (groups.some(item => item.locked_by_run)) throw new Error('研究运行正在使用该来源，请等运行结束');
    return {snapshot, source, groups};
  };
  const choose = current => {
    const group = groupId ? current.groups.find(item => item.id === groupId) :
      current.groups.length === 1 ? current.groups[0] : null;
    if (!group) throw new Error(current.groups.length > 1 ?
      '该来源有多个运行组，请在具体运行组卡片点击一键准备并启动' : '没有当前可构建的运行组，请查看候选重检结果');
    return group;
  };
  const wait = async (test, timeout) => {
    const deadline = Date.now() + timeout * 1000;
    while (true) {
      const current = await read();
      if (test(current)) return current;
      if (Date.now() >= deadline) throw new Error(progress.phase + '等待超时；已提交的后台操作继续保留，请查看实际状态');
      await new Promise(resolve => setTimeout(resolve, 1500));
    }
  };
  const action = (group, path, extra = {}) => api('/api/deployments/' + path,
    {group_id: group.id, operation_id: operationId(), ...extra});
  try {
    let current = await read();
    environment = current.source.runtime_environment;
    if (current.source.status === 'ANALYZING') throw new Error('来源仍在分析，请等待完成');
    if (!groupId && current.groups.length > 1)
      throw new Error('该来源有多个运行组，请在具体运行组卡片点击一键准备并启动');
    const changed = current.source.environment_pending_recheck || current.source.target_runtime?.status !== 'READY';
    for (const group of current.groups) {
      if (groupId && group.id !== groupId) {
        if (changed && group.instances.some(heldInstance))
          throw new Error('同来源另一运行组仍有实例，先排空停止后再调整来源环境');
        continue;
      }
      const held = group.instances.filter(heldInstance);
      if (!held.length || (!changed && !group.artifact_gap && held.every(item =>
        ['STARTING', 'UNDEPLOYED'].includes(item.state) || (item.state === 'READY' && item.accepting)))) continue;
      if (held.some(item => item.state === 'UNKNOWN') && !releaseUnknown)
        throw new Error('旧实例有未决结果；请先明确勾选释放 UNKNOWN，或手动处理后继续');
      if (held.some(item => item.in_flight && item.state !== 'UNKNOWN'))
        throw new Error('实例仍有调用或预热在途，请等其结束');
      await phase('排空并释放旧实例');
      await action(group, 'drain-stop', {release_unknown: releaseUnknown});
      await wait(value => {
        const remaining = value.groups.find(item => item.id === group.id)?.instances.filter(heldInstance) || [];
        const blocked = remaining.find(item => item.drain_blocked || item.pending_stop?.blocked);
        if (blocked) throw new Error(textValue(blocked.drain_blocked || blocked.pending_stop.blocked));
        return !remaining.length;
      }, current.snapshot.deployment_backend?.start_timeout_s || 900);
    }
    current = await read();
    if (current.source.target_runtime?.status === 'PREPARING') {
      await phase('等待目标解释器准备');
    } else if (current.source.target_runtime?.status !== 'READY') {
      await phase('准备目标解释器');
      await api('/api/harnesses/environment', {harness_id: sourceId, prepare: true});
    }
    current = await wait(value => {
      const target = value.source.target_runtime || {};
      if (target.status === 'PREPARING') return false;
      if (target.status !== 'READY') throw new Error(target.reason || '目标解释器未准备成功');
      return true;
    }, (current.snapshot.deployment_backend?.build_timeout_s || 7200) + 30);
    if (current.source.environment_pending_recheck) {
      await phase('零模型重新核对已有候选');
      await api('/api/harnesses/environment', {harness_id: sourceId, recheck: true});
      current = await read();
    }
    let group = choose(current);
    if (group.environment_gap) throw new Error(group.environment_gap);
    if (group.artifact_gap) {
      await phase('生成当前构建工件');
      await action(group, 'export', {environment});
      group = choose(await read());
    }
    for (const [field, path, label, succeeded] of [
      ['build', 'build', '构建镜像', ['BUILT', 'OPERATOR_REPORTED']],
      ['publish', 'publish', '使镜像对后端可达', ['PUBLISHED', 'NOT_REQUIRED', 'OPERATOR_REPORTED']]
    ]) {
      if (!succeeded.includes(group.artifact?.[field]?.status)) {
        await phase(label);
        if (group.artifact?.[field]?.status !== 'RUNNING') await action(group, path);
        current = await wait(value => {
          const receipt = choose(value).artifact?.[field] || {};
          if (receipt.status === 'RUNNING') return false;
          if (!succeeded.includes(receipt.status)) throw new Error(receipt.reason || label + '未完成');
          return true;
        }, (current.snapshot.deployment_backend?.build_timeout_s || 7200) + 30);
        group = choose(current);
      }
    }
    if (!group.instances.some(item => item.state === 'READY' && item.accepting)) {
      await phase('启动实例并等待就绪');
      if (!group.instances.some(item => ['STARTING', 'UNDEPLOYED'].includes(item.state))) await action(group, 'start');
      current = await wait(value => {
        const instances = choose(value).instances;
        if (instances.some(item => item.state === 'READY' && item.accepting)) return true;
        const unavailable = instances.find(item => item.state === 'UNDEPLOYED' && item.apply?.status === 'BACKEND_UNAVAILABLE');
        if (unavailable) throw new Error(unavailable.apply.reason || '启动后端不可用，请查看实际启动命令');
        const failed = instances.find(item => item.state === 'FAILED' && !item.released);
        if (failed) throw new Error(failed.reason || '实例启动失败');
        return false;
      }, (current.snapshot.deployment_backend?.start_timeout_s || 900) + 30);
      group = choose(current);
    }
    const prepared = group.instances.filter(item => item.state === 'READY' && item.accepting);
    const wanted = Object.entries(group.contract?.prepare || {}).filter(([, rule]) => rule.status === 'SUPPORTED').map(([id]) => id);
    const warm = prepared.length && prepared.every(item => wanted.every(entry =>
      (item.units || []).some(id => id.endsWith(':' + entry) && item.initialization?.[id] === 'BOUND')));
    if (wanted.length && !warm) {
      if (prepared.some(item => item.in_flight)) throw new Error('实例仍在执行调用或预热，请等结束后再继续');
      await phase('预热原构造与模块');
      const answer = await action(group, 'prewarm');
      if (answer.status !== 'PREPARED' || answer.initialization_ready !== true) {
        const missing = (answer.results || []).flatMap(item => Object.values(item.units || {}))
          .filter(item => item.status !== 'PREPARED').map(item => item.reason).filter(Boolean);
        throw new Error(missing.join('；') || '部分成员预热未完成，请查看实例记录');
      }
    }
    progress.phase = wanted.length ? '实例已就绪，允许提前准备的成员已预热' : '实例已就绪，该组无需预热';
    showNotice(progress.phase + '；原目标推理仍需实际任务验证。');
  } catch (error) {
    progress.phase = progress.phase + '：' + error.message;
    showNotice(progress.phase);
  } finally {
    progress.running = false;
    sourceUpdating.delete(sourceId);
    renderedSections.delete('harnesses'); renderedSections.delete('deployments');
    await refresh();
  }
}

function deploymentPreparationControls(container, source, group = null) {
  const progress = deploymentPreparation.get(source.id);
  const box = node('div', undefined, 'operation-item');
  const button = node('button', progress?.running ? progress.phase : '一键准备并启动', 'secondary-button');
  button.type = 'button';
  button.disabled = Boolean(progress?.running || sourceUpdating.has(source.id) || analysisSubmitting.has(source.id) ||
    group?.locked_by_run || !source.runtime_environment || source.status === 'DETACHED');
  if (!source.runtime_environment) button.title = '先保存来源运行环境；不会自动猜测依赖或调用分析模型';
  const release = node('label', undefined, 'checkbox-label');
  const input = node('input'); input.type = 'checkbox';
  const key = 'prepare-release/' + source.id;
  input.checked = deploymentDrafts.get(key) || false; input.disabled = Boolean(progress?.running);
  input.onchange = () => deploymentDrafts.set(key, input.checked);
  release.append(input, node('span', '允许释放结果未决（UNKNOWN）的旧实例'));
  button.onclick = () => prepareDeployment(source.id, group?.id, input.checked);
  box.append(button, release, node('p', '按已保存配置串行准备、重检、构建、启动和预热，已满足的步骤自动跳过。请保持本页面打开；失败后可按实际状态继续。', 'card-note'));
  if (progress && !progress.running) box.append(node('p', progress.phase, 'card-note'));
  container.append(box);
}

function deploymentButton(group, action, path, label, body = () => ({}), done = '已提交') {
  const gate = action ? group.actions?.[action] || {enabled: false, reason: '契约未提供该操作'} : {enabled: true};
  const operation = group.id + '/' + path;
  const button = node('button', label, 'secondary-button'); button.type = 'button';
  button.disabled = !gate.enabled || deploymentSubmitting.has(operation) || deploymentPreparation.get(group.source_id)?.running;
  if (gate.reason) button.title = textValue(gate.reason);
  button.onclick = async () => {
    if (deploymentSubmitting.has(operation)) return;
    deploymentSubmitting.add(operation);
    button.disabled = true;
    try {
      await api('/api/deployments/' + path, {group_id: group.id, operation_id: operationId(), ...body()});
      showNotice(done);
    } catch (error) { showNotice(error.message); }
    finally { deploymentSubmitting.delete(operation); renderedSections.delete('deployments'); await refresh(); }
  };
  return button;
}

function renderInstance(card, instance, sourceId) {
  const box = node('div', undefined, 'operation-item');
  const head = node('div', undefined, 'card-heading');
  head.append(node('strong', instance.name || instance.id),
    node('span', instanceStateNames[instance.state] || instance.state, 'status-pill ' + statusClass(
      instance.state === 'READY' ? 'READY' : ['FAILED', 'UNKNOWN'].includes(instance.state) ? 'FAILED' :
      ['STARTING', 'DRAINING'].includes(instance.state) ? 'RUNNING' : 'PENDING')));
  box.append(head, node('p', ['代次 ' + instance.generation, instance.origin === 'OPERATOR_ATTACHED' ? '操作者启动后登记' : '平台启动',
    instance.accepting ? '接单中' : '不接单', '在途 ' + (instance.in_flight || 0),
    instance.run_id ? '运行 ' + instance.run_id.slice(-12) : null].filter(Boolean).join(' · '), 'card-meta'));
  if (instance.reason) box.append(node('p', instance.reason, 'card-note'));
  if (instance.drain_blocked || instance.pending_stop?.blocked) box.append(node('p', '停止受阻：' + textValue(instance.drain_blocked || instance.pending_stop.blocked), 'card-note'));
  if (instance.pending_stop && !instance.released) box.append(node('p', '待释放：尚未确认后端对象已删除。', 'card-note'));
  if (instance.apply?.manual_commands) detailValue(box, '后端不可用 · 手动命令', instance.apply.manual_commands);
  if (instance.stop_result?.manual_commands) detailValue(box, '待释放 · 手动操作与确认命令', instance.stop_result.manual_commands);
  detailValue(box, '实例详情', {endpoint: instance.endpoint, image: instance.image, image_id: instance.image_id,
    units: instance.units, missing: instance.missing, initialization: instance.initialization,
    environment: instance.environment, resources: instance.resources, observed: instance.observed,
    apply: instance.apply, stop_result: instance.stop_result});
  detailValue(box, '最近事件', instance.recent_events);
  if (inspectPage && ['STOPPED', 'FAILED'].includes(instance.state)) {
    const cleanup = node('button', '清理记录', 'secondary-button'); cleanup.type = 'button';
    cleanup.disabled = !instance.can_cleanup || deploymentPreparation.get(sourceId)?.running;
    if (instance.cleanup_reason) cleanup.title = instance.cleanup_reason;
    cleanup.onclick = async () => {
      cleanup.disabled = true;
      try {
        await api('/api/deployments/clear-history', {group_id: instance.group_id, instance_id: instance.id});
        showNotice('已清理面板记录；原代次与研究成本证据保留。');
      } catch (error) { showNotice(error.message); }
      finally { renderedSections.delete('deployments'); await refresh(); }
    };
    box.append(cleanup);
  }
  card.append(box);
}

function renderDeploymentBackend(root) {
  const card = node('article', undefined, 'status-card');
  card.dataset.itemId = 'deployment-backend';
  const backend = state.deployment_backend || {kind: 'none'};
  const head = node('div', undefined, 'card-heading');
  head.append(node('strong', '部署后端'), node('span', backend.kind === 'none' ? '未配置' : backend.kind, 'status-pill pending'));
  card.append(head, node('p', '平台通过 docker / kubectl / kind 命令执行构建、发布与启停；命令不可用时给出手动命令。镜像不含权重与凭据；凭据只以环境变量名或 Secret 引用注入。', 'card-note'));
  const controller = state.runtime_controller || {};
  if (controller.error) card.append(node('p', '控制器错误：' + textValue(controller.error), 'card-note'));
  if (controller.locks && Object.keys(controller.locks).length) detailValue(card, '运行占用的运行组', controller.locks);
  const form = node('form', undefined, 'model-form');
  const label = node('label', '后端配置 JSON');
  const input = node('textarea'); input.rows = 8;
  const {updated_at, ...editable} = backend;
  input.value = deploymentDrafts.get('backend') ?? JSON.stringify(editable, null, 2);
  input.dataset.deploymentField = 'backend';
  input.oninput = () => deploymentDrafts.set('backend', input.value);
  label.append(input);
  const button = node('button', '保存部署后端', 'secondary-button'); button.type = 'submit';
  button.disabled = deploymentSubmitting.has('backend') || [...deploymentPreparation.values()].some(item => item.running);
  form.append(label, button);
  form.onsubmit = async event => {
    event.preventDefault();
    if (deploymentSubmitting.has('backend')) return;
    deploymentSubmitting.add('backend'); button.disabled = true;
    try { await api('/api/deployments/backend', JSON.parse(input.value)); deploymentDrafts.delete('backend'); showNotice('部署后端已保存。'); }
    catch (error) { showNotice(error.message); }
    finally { deploymentSubmitting.delete('backend'); renderedSections.delete('deployments'); await refresh(); }
  };
  card.append(form);
  root.append(card);
}

function renderDeployments() {
  const root = $('deployment-list');
  root.replaceChildren();
  const groups = (state.runtime_groups || []).filter(group => !selectedSource() || group.source_id === selectedSource())
    .sort((a, b) => Number(b.host_status === 'HOST_CHECKED') - Number(a.host_status === 'HOST_CHECKED'));
  root.append(node('p', '同组保持原进程、原对象与原生命周期；组外端口只传编码后的数据。生命周期操作按运行组契约逐项允许，未知不授予；重启不恢复原会话。', 'card-note'));
  if (inspectPage) renderDeploymentBackend(root);
  if (!groups.length) return empty(root, '分析后显示运行组。');
  for (const group of groups) {
    const card = node('article', undefined, 'status-card');
    card.dataset.itemId = group.id;
    const head = node('div', undefined, 'card-heading');
    head.append(node('strong', group.id), pill(group.host_status === 'HOST_CHECKED' ? 'HOST_CHECKED' : group.status));
    const live = (group.instances || []).filter(item => !['STOPPED', 'FAILED'].includes(item.state));
    card.append(head, node('p', ['宿主：' + (group.contract?.host_profile || '未生成契约'),
      '执行位置：' + statusLabel(group.execution), '存活实例 ' + live.length,
      group.contract?.replicas?.replicable ? '可增加副本' : '单实例', group.locked_by_run ? '运行占用中' : null].filter(Boolean).join(' · '), 'card-meta'));
    if (group.environment_gap) card.append(node('p', group.environment_gap, 'card-note message-error'));
    card.append(node('p', '成员：' + group.entries.length + ' 个' + (group.entries.length <= 3 ? ' · ' + group.entries.join('、') : '，展开宿主核对查看完整成员。'), 'card-note'));
    if (group.host) detailValue(card, '宿主核对（逐成员）', group.host.members);
    if (group.contract) {
      const list = node('div', undefined, 'operation-list');
      for (const [name, operation] of Object.entries(group.contract.operations)) {
        const item = node('div', undefined, 'operation-item');
        item.append(node('strong', name + ' · ' + (operationNames[operation.status] || operation.status)), node('p', operation.basis, 'card-note'));
        list.append(item);
      }
      const details = node('details'); details.append(node('summary', '运行组契约 · ' + group.contract.digest.slice(0, 12)), list);
      card.append(details);
      if (group.contract.lifecycle) detailValue(card, '生命周期分项资格（UNKNOWN 不授予动作）', group.contract.lifecycle);
      detailValue(card, '契约原文', group.contract);
    }
    if (group.gaps?.length) detailValue(card, '未覆盖的标量远程条件', [...new Set(group.gaps)]);
    const artifact = group.artifact;
    const source = (state.harnesses || []).find(item => item.id === group.source_id);
    const discovery = group.environment_discovery || source?.environment_discovery;
    environmentEvidence(card, discovery);
    if (source?.runtime_environment) card.append(node('p', '来源已选环境：Python ' + source.runtime_environment.python +
      ' · ' + source.runtime_environment.base_image, 'card-meta'));
    if (artifact) {
      card.append(node('p', ['工件 ' + statusLabel(artifact.status), '构建 ' + (artifact.build?.status || 'NOT_RUN'),
        '镜像可达 ' + (artifact.publish?.status || 'NOT_RUN'), artifact.publish?.reference || artifact.image].join(' · '), 'card-meta'));
      if (artifact.build?.reason || artifact.publish?.reason) card.append(node('p', textValue(artifact.build?.reason || artifact.publish?.reason), 'card-note'));
      for (const step of [artifact.build, artifact.publish]) if (step?.manual_commands) detailValue(card, '后端不可用 · 手动命令', step.manual_commands);
      detailValue(card, '构建工件', artifact);
      if (artifact.build_reuse) {
        const reuse = artifact.build?.dependency_reuse;
        const result = {REUSED: '已复用安装环境', BUILT: '已构建安装环境', FAILED: '依赖环境准备失败',
          NOT_CHECKED: '尚未核验安装环境', NOT_APPLICABLE: '无独立依赖环境'};
        card.append(node('p', '依赖环境：' + (result[reuse?.status] || '待构建核验') +
          (reuse?.dependency_image_id ? ' · ' + reuse.dependency_image_id : '') +
          '。下载缓存是否命中需查看实际构建日志。', 'card-note'));
        if (reuse?.reason) card.append(node('p', reuse.reason, 'card-note'));
        if (artifact.build_reuse.installation) detailValue(card, '依赖安装顺序及原因', artifact.build_reuse.installation);
      }
    }
    card.append(node('p', '真实 worker：' + ((group.instances || []).some(item => item.state === 'READY' && item.accepting) ?
      '已有就绪且接单的实例' : '尚无就绪且接单的实例') + '。生成工件、构建成功、镜像可达与实例就绪分别记录。', 'card-note'));
    if (inspectPage && group.contract) {
      const key = 'group/' + group.id;
      const environment = artifact?.environment || source?.runtime_environment || discovery?.suggested || {};
      const {form, fields, read, load} = environmentEditor(environment, key);
      const editingLocked = Boolean(group.locked_by_run || group.environment_gap) || deploymentSubmitting.has(key) ||
        deploymentPreparation.get(group.source_id)?.running;
      for (const field of Object.values(fields)) field.disabled = editingLocked;
      form.prepend(node('h4', '选择构建环境'), node('p', '基础镜像会实际用于镜像构建；目标 Python 需与镜像内解释器一致。改变环境后须重新核对与构建，已有实例保持原环境。', 'card-note'));
      if (source?.runtime_environment) {
        const useSource = node('button', '填写来源已选环境', 'secondary-button'); useSource.type = 'button'; useSource.disabled = editingLocked;
        useSource.onclick = () => load(source.runtime_environment); form.append(useSource);
      }
      const button = node('button', '生成构建工件', 'secondary-button'); button.type = 'submit';
      button.disabled = !group.actions?.export?.enabled || editingLocked;
      if (group.actions?.export?.reason || group.locked_by_run) button.title = group.actions?.export?.reason || '研究运行正在使用此组，结束后再修改环境';
      form.append(button);
      form.onsubmit = async event => {
        event.preventDefault();
        if (editingLocked || deploymentSubmitting.has(key)) return;
        deploymentSubmitting.add(key); button.disabled = true;
        for (const field of Object.values(fields)) field.disabled = true;
        try {
          const result = await api('/api/deployments/export', {group_id: group.id, environment: read()});
          deploymentDrafts.delete(key);
          showNotice('构建工件已生成：' + result.image + '。尚未构建镜像。');
        } catch (error) { showNotice(error.message); }
        finally { deploymentSubmitting.delete(key); renderedSections.delete('deployments'); await refresh(); }
      };
      card.append(form);
      if (source) deploymentPreparationControls(card, source, group);
      if (source) {
        const environmentLink = node('a', '调整来源环境与目标解释器', 'detail-link');
        environmentLink.href = '/configuration?section=harnesses'; card.append(environmentLink);
      }
      const actions = node('div', undefined, 'model-actions');
      const release = node('label', undefined, 'checkbox-label');
      const releaseInput = node('input'); releaseInput.type = 'checkbox';
      releaseInput.checked = deploymentDrafts.get('release/' + group.id) || false;
      releaseInput.dataset.deploymentField = 'release/' + group.id;
      releaseInput.onchange = () => deploymentDrafts.set('release/' + group.id, releaseInput.checked);
      release.append(releaseInput, node('span', '停止时释放结果未决（UNKNOWN）的实例'));
      actions.append(
        deploymentButton(group, 'build', 'build', '构建镜像', () => ({}), '构建已在后台开始。'),
        deploymentButton(group, 'publish', 'publish', '使镜像对后端可达', () => ({}), '发布已在后台开始。'),
        deploymentButton(group, 'start', 'start', '启动实例', () => ({}), '实例启动已提交；进程登记后转为就绪。'),
        deploymentButton(group, 'prewarm', 'prewarm', '预热', () => ({}), '预热已完成，结果见实例事件。'),
        deploymentButton(group, 'pause_intake', 'pause', '停止接单', () => ({}), '已停止派发新根请求。'),
        deploymentButton(group, 'drain_stop', 'drain-stop', '排空后停止', () => ({release_unknown: releaseInput.checked}), '已请求排空；收尾后停止。'),
        deploymentButton(group, 'add_replica', 'add-replica', '增加副本', () => ({}), '新副本启动已提交。'),
        deploymentButton(group, 'remove_replica', 'remove-replica', '减少副本', () => ({}), '已请求排空一个副本。'),
        deploymentButton(group, null, 'status', '刷新后端观测', () => ({}), '已刷新实例观测。'));
      card.append(actions, release);
      const cleanup = deploymentButton(group, null, 'clear-history', '清理已释放记录', () => ({}),
        '已清理面板中的已释放记录；原始代次与研究证据保留。');
      cleanup.disabled ||= !group.history_cleanup?.enabled;
      if (group.history_cleanup?.reason) cleanup.title = group.history_cleanup.reason;
      card.append(cleanup);
      const reported = node('form', undefined, 'model-form');
      const reportedLabel = node('label', '平台外已构建的镜像引用（操作者报告）');
      const reportedInput = node('input'); reportedInput.placeholder = 'registry.example/seampact-worker:tag';
      const reportedKey = 'reported/' + group.id;
      reportedInput.value = deploymentDrafts.get(reportedKey) || '';
      reportedInput.dataset.deploymentField = reportedKey;
      reportedInput.oninput = () => deploymentDrafts.set(reportedKey, reportedInput.value);
      reportedLabel.append(reportedInput);
      const reportedButton = node('button', '记录操作者构建的镜像', 'secondary-button'); reportedButton.type = 'submit';
      reportedButton.disabled = !group.actions?.build?.enabled || Boolean(group.locked_by_run) || deploymentSubmitting.has(reportedKey);
      reported.append(reportedLabel, reportedButton);
      reported.onsubmit = async event => {
        event.preventDefault();
        if (deploymentSubmitting.has(reportedKey)) return;
        deploymentSubmitting.add(reportedKey); reportedButton.disabled = true;
        try { await api('/api/deployments/build', {group_id: group.id, operator_reported_image: reportedInput.value.trim()});
          deploymentDrafts.delete(reportedKey); showNotice('已记录；平台未核对该构建。'); }
        catch (error) { showNotice(error.message); }
        finally { deploymentSubmitting.delete(reportedKey); renderedSections.delete('deployments'); await refresh(); }
      };
      card.append(reported);
    }
    for (const instance of group.instances || []) renderInstance(card, instance, group.source_id);
    if (!inspectPage) card.append(inspectLink('deployments', {harness: group.source_id}));
    root.append(card);
  }
  if (inspectPage) {
    const form = node('form', undefined, 'model-form');
    const label = node('label', '平台访问运行进程的地址');
    const input = node('input'); input.type = 'url'; input.required = true; input.placeholder = 'http://127.0.0.1:8890';
    input.value = deploymentDrafts.get('attach') || ''; input.dataset.deploymentField = 'attach';
    input.oninput = () => deploymentDrafts.set('attach', input.value);
    label.append(input);
    const button = node('button', '登记已启动的运行进程', 'secondary-button'); button.type = 'submit';
    button.disabled = deploymentSubmitting.has('attach');
    form.append(label, button);
    form.onsubmit = async event => {
      event.preventDefault();
      if (deploymentSubmitting.has('attach')) return;
      deploymentSubmitting.add('attach'); button.disabled = true;
      try { await api('/api/workers/attach', {endpoint: input.value}); deploymentDrafts.delete('attach'); showNotice('已登记为实例；已登记成员的调用将路由到该实例。'); }
      catch (error) { showNotice(error.message); }
      finally { deploymentSubmitting.delete('attach'); renderedSections.delete('deployments'); await refresh(); }
    };
    root.append(form);
  }
}

function updateAnalysisClocks() {
  for (const element of document.querySelectorAll('[data-analysis-operation-start]')) {
    const started = Date.parse(element.dataset.analysisOperationStart);
    if (Number.isFinite(started)) element.textContent = element.dataset.analysisOperationLabel +
      ' · 已等待 ' + (Math.max(0, Date.now() - started) / 1000).toFixed(0) + ' 秒';
  }
}

function analysisTimingDetails(root, job, timings) {
  const models = timings.filter(item => item.phase === 'model_request');
  const records = Array.isArray(job.usage) ? job.usage : [];
  if (models.length) {
    const slowest = [...models].sort((left, right) => (right.duration_ms || 0) - (left.duration_ms || 0))[0];
    root.append(node('p', '最慢模型请求：第 ' + slowest.round + ' 轮 · ' +
      ((slowest.duration_ms || 0) / 1000).toFixed(1) + ' 秒', 'card-meta'));
    const detail = node('details'); detail.append(node('summary', '逐轮模型耗时与输出 · ' + models.length + ' 次请求'));
    const table = node('table', undefined, 'research-metrics analysis-usage');
    const head = node('tr');
    for (const name of ['轮次 / 阶段', '等待秒', '输出 token', '其中推理', '工具数']) head.append(node('th', name));
    const thead = node('thead'); thead.append(head); table.append(thead);
    const body = node('tbody');
    models.forEach((item, index) => {
      const usage = records[index] || {};
      const output = item.output_tokens ?? usage.completion_tokens ?? usage.output_tokens;
      const reasoning = item.reasoning_tokens ?? usage.completion_tokens_details?.reasoning_tokens ?? usage.output_tokens_details?.reasoning_tokens;
      const row = node('tr');
      row.append(node('td', item.round + ' / ' + ({preprocessing: '预处理', analysis: '拆分'}[item.stage] || item.stage || '未记录')),
        node('td', ((item.duration_ms || 0) / 1000).toFixed(1)),
        node('td', Number.isFinite(output) ? output.toLocaleString('zh-CN') : '未报告'),
        node('td', Number.isFinite(reasoning) ? reasoning.toLocaleString('zh-CN') : '未报告'),
        node('td', Number.isFinite(item.tool_calls) ? item.tool_calls : '未记录'));
      body.append(row);
    });
    table.append(body); detail.append(table,
      node('p', '请求等待包含服务端排队、网络传输与生成；非流式记录无法将三者分别计时。', 'card-note'));
    root.append(detail);
  }
  const tools = timings.filter(item => item.tool).sort((left, right) => (right.duration_ms || 0) - (left.duration_ms || 0));
  if (tools.length) {
    const detail = node('details'); detail.append(node('summary', '工具耗时 · 按耗时降序'));
    const table = node('table', undefined, 'research-metrics analysis-usage');
    const head = node('tr');
    for (const name of ['轮次', '工具', '耗时秒', '结果']) head.append(node('th', name));
    const thead = node('thead'); thead.append(head); table.append(thead);
    const body = node('tbody');
    for (const item of tools) {
      const row = node('tr');
      row.append(node('td', item.round), node('td', item.tool), node('td', ((item.duration_ms || 0) / 1000).toFixed(3)),
        node('td', {completed: '已返回', failed: '失败', cancelled: '已取消'}[item.outcome] || '未记录'));
      body.append(row);
    }
    table.append(body); detail.append(table); root.append(detail);
  }
  detailValue(root, '耗时原始字段', timings);
}

function analysisProgress(job) {
  const root = node('div', undefined, 'analysis-progress');
  const preparation = job.preprocessing || {};
  const finished = job.status === 'COMPLETED';
  const active = ['RUNNING', 'QUEUED'].includes(job.status);
  const reusedCandidate = job.model_calls === 0 || ['IMPORTED_ANALYSIS', 'ANALYZER_RESOURCE_BINDING', 'EXPLICIT_RESOURCE_BINDING'].includes(job.kind);
  const steps = [
    ['自动预处理', reusedCandidate || ['SKIPPED', 'REPLAYED'].includes(preparation.status) ? 'reused' : preparation.status === 'COMPLETED' ? 'done' :
      active && job.pipeline_stage === 'preprocessing' ? 'active' : 'pending'],
    ['入口与拆分', reusedCandidate ? 'reused' : finished || job.submitted ? 'done' : active && job.pipeline_stage === 'analysis' ? 'active' : 'pending'],
    ['规则与绑定', finished ? 'done' : job.submitted && active ? 'active' : 'pending']
  ];
  const stages = node('ol', undefined, 'analysis-stages');
  for (const [label, status] of steps) {
    const step = node('li', undefined, 'analysis-stage'); step.dataset.state = status;
    step.append(node('strong', label), node('span', {done: '已记录', active: '进行中', reused: reusedCandidate ? '沿用已有分析' :
      preparation.status === 'REPLAYED' ? '复用已核对结果' : '本次跳过', pending: '等待'}[status]));
    stages.append(step);
  }
  root.append(stages);
  if (job.phase) root.append(node('p', (active || job.status === 'CANCELLING' ? '当前操作：' : '最后操作：') + ({model_request: '等待模型响应', source_tools: '读取源码与核对候选',
    binding_installation: '安装资源绑定', installation: '安装能力入口'}[job.phase] || job.phase), 'card-meta'));
  if (job.status === 'CANCELLING') root.append(node('p', '正在结束本次分析；取消后保留已保存进度，不再安装本次候选。', 'card-note'));
  const operation = job.active_operation;
  if (operation?.started_at && active) {
    const clock = node('p', undefined, 'card-meta');
    clock.dataset.analysisOperationStart = operation.started_at;
    clock.dataset.analysisOperationLabel = operation.phase === 'model_request' ? '第 ' + operation.round + ' 轮模型请求' :
      operation.tool ? '正在执行 ' + operation.tool : '正在核对 ' + (operation.phase || '分析条件');
    root.append(clock);
    const started = Date.parse(operation.started_at);
    clock.textContent = clock.dataset.analysisOperationLabel + (Number.isFinite(started) ?
      ' · 已等待 ' + (Math.max(0, Date.now() - started) / 1000).toFixed(0) + ' 秒' : '');
  }
  const timings = Array.isArray(job.timings) ? job.timings : [];
  if (timings.length) {
    const milliseconds = value => (value / 1000).toFixed(1) + ' 秒';
    const modelTime = timings.filter(item => item.phase === 'model_request').reduce((sum, item) => sum + (item.duration_ms || 0), 0);
    const toolTime = timings.filter(item => item.tool).reduce((sum, item) => sum + (item.duration_ms || 0), 0);
    const latest = timings.filter(item => item.phase === 'model_request').at(-1);
    root.append(node('p', '模型累计等待 ' + milliseconds(modelTime) + ' · 工具累计 ' + milliseconds(toolTime) +
      (latest ? ' · 最近一轮模型 ' + milliseconds(latest.duration_ms || 0) : ''), 'card-meta'));
    if (inspectPage) analysisTimingDetails(root, job, timings);
  }
  const usage = Array.isArray(job.usage) ? job.usage : [];
  if (!usage.length && reusedCandidate) {
    root.append(node('p', '本次沿用已有候选，仅检查或安装接入结果；未调用分析模型。', 'card-note'));
    return root;
  }
  const table = node('table', undefined, 'research-metrics analysis-usage');
  const header = node('tr');
  for (const label of ['阶段', '响应记录', '输入 token', '输出 token', '总 token']) header.append(node('th', label));
  const thead = node('thead'); thead.append(header); table.append(thead);
  const tbody = node('tbody');
  const groups = job.usage_by_stage && Object.keys(job.usage_by_stage).length ? Object.entries(job.usage_by_stage) : [['all', usage]];
  for (const [stage, records] of groups) {
    if (!Array.isArray(records)) continue;
    const row = node('tr');
    row.append(node('td', {preprocessing: '预处理', analysis: '拆分分析', all: '本次记录'}[stage] || stage), node('td', records.length));
    for (const field of ['prompt_tokens', 'completion_tokens', 'total_tokens']) {
      const alias = {prompt_tokens: 'input_tokens', completion_tokens: 'output_tokens'}[field];
      const values = records.map(item => item?.[field] ?? item?.[alias]);
      const known = values.filter(value => finiteNumber(value));
      const total = known.reduce((sum, value) => sum + value, 0);
      row.append(node('td', !known.length ? 'UNKNOWN' : total.toLocaleString('zh-CN') + (known.length < records.length ? '（已报告部分）' : '')));
    }
    tbody.append(row);
  }
  table.append(tbody); root.append(table);
  root.append(node('p', '用量随模型响应更新；等待中的请求可能尚无用量。阶段已记录不代表资源齐备或原目标已运行。', 'card-note'));
  return root;
}

function analysisExportButton(job, candidate = false) {
  const button = node('button', candidate ? '导出完成候选' : '导出当前进度', 'secondary-button'); button.type = 'button';
  button.onclick = async () => {
    button.disabled = true;
    try {
      const result = candidate ? await api('/api/analysis/export', {analysis_id: job.id}) :
        await api('/api/analysis/' + encodeURIComponent(job.id) + '/snapshot');
      const value = {...result}; delete value.path;
      downloadJson(value, 'analysis-' + job.id + (candidate ? '-candidate' : '-progress') + '.json');
      showNotice(candidate ? (value.absolute_path_locations?.length ? '候选已导出，但包含本机绝对路径；迁移须从未绑定配置的原分析导出。' :
        '已导出完成候选；目标平台仍需核对源码并重新绑定资源。') : '已导出当前已保存进度；进度快照用于观察，不是可恢复或导入的分析工件。');
    } catch (error) { showNotice(error.message); }
    finally { button.disabled = false; }
  };
  return button;
}

function revisionResultBasis(revision) {
  return {candidate_digest: revision.candidate_digest, source_version: revision.source_version,
    installed_analysis_id: revision.installed_analysis_id || null};
}

function acceptRevision(revision) {
  revisionDetails.set(revision.id, revision);
  const prior = revisionDrafts.get(revision.id);
  if (!prior?.dirty) revisionDrafts.set(revision.id, {
    candidate: textValue(revision.candidate), guidance: revision.guidance || '', reason: revision.reason || '', dirty: false
  });
  const feedback = revisionFeedbackDrafts.get(revision.id) || {};
  if (!feedback.interventionDirty) {
    const saved = JSON.parse(JSON.stringify(revision.intervention || {}));
    feedback.intervention = {
      actor: {kind: 'user', name: ''}, cause: 'unknown', change_target: 'unknown', summary: '', evidence: '',
      ...saved,
      generalization: {disposition: 'needs_review', pattern_key: '', target: 'unknown', status: 'open',
        resolution: '', version: '', evidence: '', ...(saved.generalization || {})}
    };
    // This form describes the next intervention, not the author of old records.
    feedback.intervention.actor = {kind: 'user', name: ''};
    feedback.interventionDirty = false;
  }
  if (!feedback.resultDirty) {
    feedback.result = {status: 'unknown', summary: '', evidence: '', ...(revision.result || {})};
    feedback.resultBasis = revisionResultBasis(revision);
    feedback.resultDirty = false;
  }
  revisionFeedbackDrafts.set(revision.id, feedback);
}

async function refreshRevisionDetails() {
  if (!inspectPage || inspectView !== 'analyses') return;
  const selected = new Set(revisionSelected.values());
  const results = await Promise.allSettled((state.analysis_revisions || []).filter(item => selected.has(item.id)).map(async item => {
    const version = JSON.stringify(item);
    if (revisionVersions.get(item.id) === version || revisionBusy.has(item.id)) return;
    try {
      const result = await api('/api/analysis/revisions/' + encodeURIComponent(item.id));
      acceptRevision(result.revision || result);
      revisionVersions.set(item.id, version);
      revisionErrors.delete(item.id);
    } catch (error) { revisionErrors.set(item.id, error.message); }
  }));
  for (const result of results) if (result.status === 'rejected') showNotice(result.reason?.message || String(result.reason));
}

function revisionInput(root, label, value, key, oninput, multiline = false) {
  const wrapper = node('label', undefined, 'revision-field');
  wrapper.append(node('span', label));
  const input = node(multiline ? 'textarea' : 'input');
  input.value = value;
  input.dataset.deploymentField = 'revision:' + key;
  input.oninput = () => oninput(input.value);
  wrapper.append(input); root.append(wrapper);
  return input;
}

function revisionSelect(root, label, value, key, options, onchange) {
  const wrapper = node('label', undefined, 'revision-field'); wrapper.append(node('span', label));
  const select = node('select'); select.dataset.deploymentField = 'revision:' + key;
  for (const [code, text] of options) {
    const option = node('option', text); option.value = code; select.append(option);
  }
  select.value = value; select.onchange = () => onchange(select.value);
  wrapper.append(select); root.append(wrapper); return select;
}

async function saveRevisionFeedback(id) {
  if (revisionBusy.has(id)) return;
  revisionBusy.add(id); revisionErrors.delete(id); renderedSections.delete('analyses');
  await refresh();
  try {
    const draft = revisionFeedbackDrafts.get(id);
    const response = await api('/api/analysis/revisions/feedback', {
      revision_id: id, intervention: draft.intervention, result: draft.result, expected_basis: draft.resultBasis
    });
    draft.interventionDirty = false; draft.resultDirty = false;
    acceptRevision(response.revision); revisionVersions.delete(id);
    showNotice('介入、结果与共性处理记录已保存；候选和能力池保持原版本。');
  } catch (error) { revisionErrors.set(id, error.message); }
  finally { revisionBusy.delete(id); renderedSections.delete('analyses'); await refresh(); }
}

function renderRevisionFeedback(workspace, revision) {
  const id = revision.id, draft = revisionFeedbackDrafts.get(id), record = draft.intervention;
  const group = record.generalization;
  const section = node('section', undefined, 'revision-feedback'); section.append(node('h4', '介入与共性问题记录'));
  section.append(node('p', '记录实际由谁改了什么，并区分当前个例的结果与共性改进。历史来源未记载的仍为未知；以下结果由填写者声明，不替代原目标执行记录。', 'card-note'));
  const fields = node('div', undefined, 'revision-feedback-grid'), inputs = [];
  const change = (object, key, value, result = false) => {
    object[key] = value; draft[result ? 'resultDirty' : 'interventionDirty'] = true; update();
  };
  const select = (root, label, object, key, options, result = false) => inputs.push(revisionSelect(root, label,
    object[key], id + ':feedback:' + (result ? 'result:' : '') + key, options, value => change(object, key, value, result)));
  const input = (root, label, object, key, multiline = false, result = false) => inputs.push(revisionInput(root, label,
    object[key] || '', id + ':feedback:' + (result ? 'result:' : object === group ? 'generalization:' : '') + key,
    value => change(object, key, value, result), multiline));
  select(fields, '本次介入的实际执行者', record.actor, 'kind', [['user', '用户'], ['external_agent', '外部 Agent'], ['unknown', '尚不确定']]);
  input(fields, '执行者说明（如 Agent 名称）', record.actor, 'name');
  select(fields, '问题原因', record, 'cause', [['unknown', '尚未归因'], ['resource_configuration', '资源或配置'],
    ['discovery_gap', '分析发现遗漏'], ['unsupported_rule', '规则尚不支持'], ['checker_defect', '检查器缺陷'],
    ['build_defect', '构建器缺陷'], ['semantic_mismatch', '业务或接口含义不匹配']]);
  select(fields, '实际修改对象', record, 'change_target', [['unknown', '尚未确定'], ['configuration', '配置'],
    ['candidate', '分析候选'], ['source', '原源码'], ['platform', '平台实现']]);
  section.append(fields);
  input(section, '具体如何介入', record, 'summary', true);
  input(section, '修改依据（源码位置、差异或记录引用）', record, 'evidence', true);
  section.append(node('p', '原源码改动须重新冻结来源；此处记录不直接修改原源码或放行候选。', 'card-note'));
  const common = node('details'); common.append(node('summary', '共性归纳与改进跟踪'));
  select(common, '处理去向', group, 'disposition', [['needs_review', '待判断是否具有共性'], ['case_specific', '保留为个例适配'], ['generalizable', '需要改进通用能力']]);
  input(common, '同类问题标识（相同标识关联其他修订）', group, 'pattern_key');
  select(common, '应改进的部分', group, 'target', [['unknown', '尚未确定'], ['analysis', '分析动作'], ['rule', '绑定或分析规则'],
    ['checker', '检查器'], ['builder', '构建器'], ['theory', '理论或语义规格']]);
  select(common, '共性问题处理状态（填写者声明）', group, 'status', [['open', '待处理'], ['implemented', '已实现，待验证'], ['verified', '已验证，附证据']]);
  input(common, '通用改进内容', group, 'resolution', true);
  input(common, '对应实现或规则版本', group, 'version');
  input(common, '验证依据与适用范围', group, 'evidence', true);
  common.append(node('p', '当前个例跑通不会自动关闭共性问题；状态和证据仅用于跟踪，不自动发布新规则。', 'card-note'));
  section.append(common);
  const resultFields = node('details'); resultFields.append(node('summary', '当前个例结果（安装后可补记）'));
  const resultStale = JSON.stringify(draft.resultBasis) !== JSON.stringify(revisionResultBasis(revision));
  if (resultStale) {
    resultFields.open = true;
    resultFields.append(node('p', '候选、来源或安装版本已变化。当前结果草稿属于旧版本，请先作废旧结果，再为当前版本填写。介入说明会保留。', 'message-error'));
    const reset = node('button', '作废旧结果草稿，重新填写', 'secondary-button'); reset.type = 'button';
    reset.disabled = revisionBusy.has(id) || revision.status === 'ANALYZING';
    reset.onclick = async () => {
      draft.result = {status: 'unknown', summary: '', evidence: ''};
      draft.resultBasis = revisionResultBasis(revision); draft.resultDirty = true;
      revisionErrors.delete(id); renderedSections.delete('analyses'); await refresh();
    };
    resultFields.append(reset);
  }
  select(resultFields, '个例结果（填写者声明）', draft.result, 'status', [['unknown', '尚未确认'], ['succeeded', '目标成功'], ['partial', '部分完成'], ['failed', '目标失败']], true);
  input(resultFields, '完成范围与剩余问题', draft.result, 'summary', true, true);
  input(resultFields, '实际运行记录或结果依据', draft.result, 'evidence', true, true);
  section.append(resultFields);
  const feedback = node('p', undefined, 'card-note'), save = node('button', '保存介入与处理记录', 'secondary-button'); save.type = 'button';
  save.onclick = () => saveRevisionFeedback(id);
  function update() {
    feedback.textContent = draft.interventionDirty || draft.resultDirty ? '有未保存的介入或结果记录。' : '介入记录与候选检查分别保存。';
    const busy = revisionBusy.has(id) || revision.status === 'ANALYZING';
    for (const field of inputs) field.disabled = busy;
    save.disabled = busy || resultStale || !(draft.interventionDirty || draft.resultDirty);
  }
  update(); section.append(feedback, save);
  const related = revision.related_revision_ids || (state.analysis_revisions || []).find(item => item.id === id)?.related_revision_ids || [];
  if (related.length) {
    const links = node('div', undefined, 'harness-actions');
    links.append(node('span', '同类问题记录：', 'card-note'));
    for (const otherId of related) {
      const other = (state.analysis_revisions || []).find(item => item.id === otherId);
      const link = node('a', otherId, 'detail-link');
      link.href = '/inspect?' + new URLSearchParams({view: 'analyses', ...(other?.harness_id ? {harness: other.harness_id} : {}), revision: otherId});
      links.append(link);
    }
    section.append(links);
  }
  detailValue(section, '已保存介入与个例结果', {intervention: revision.intervention, result: revision.result,
    result_basis: revision.result_basis});
  workspace.append(section);
}

async function revisionAction(id, operation) {
  if (revisionBusy.has(id)) return;
  revisionBusy.add(id); revisionErrors.delete(id);
  renderedSections.delete('analyses');
  await refresh();
  try {
    const draft = revisionDrafts.get(id);
    const feedback = revisionFeedbackDrafts.get(id);
    const body = {revision_id: id, intervention: feedback.intervention};
    if (operation !== 'install') {
      const candidate = JSON.parse(draft.candidate);
      if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) throw new Error('候选必须是 JSON 对象。');
      Object.assign(body, {candidate, guidance: draft.guidance, reason: draft.reason});
      if (Array.isArray(draft.selectedEntryIds)) body.selected_entry_ids = draft.selectedEntryIds;
    }
    const result = await api('/api/analysis/revisions/' + operation, body);
    draft.dirty = false;
    if (operation !== 'install') feedback.interventionDirty = false;
    if (result.revision) acceptRevision(result.revision);
    revisionVersions.delete(id);
    showNotice(operation === 'check' && result.revision?.status !== 'CHECKED' ?
      '草稿已保存，但完整重检失败：' + (textValue(result.revision?.error) || '请查看检查反馈。') :
      {save: '草稿已保存，当前能力池未变。', check: '已保存并完成完整重检；逐项可用条件请查看报告。',
      refine: '已启动模型局部修复；本次使用当前分析模型，用量单独记录，完成后仍需检查和安装。',
      install: '新候选已独立安装，原分析历史保留。'}[operation]);
  } catch (error) { revisionErrors.set(id, error.message); }
  finally {
    revisionBusy.delete(id); renderedSections.delete('analyses'); await refresh();
  }
}

function renderRevisionEntrySelection(root, draft, locked, changed) {
  root.replaceChildren();
  root.append(node('h4', '选择本次接入的入口'));
  root.append(node('p', '取消勾选可移除不需要的入口，包括未接通的入口。先保存并检查，再安装才会改变能力池；原源码和内部必需调用不会被删除。', 'card-note'));
  let candidate;
  try { candidate = JSON.parse(draft.candidate); }
  catch { root.append(node('p', '候选内容暂时无法读取，请先修正高级编辑中的 JSON。', 'card-note')); return; }
  const candidates = candidate?.entries;
  if (!Array.isArray(candidates) || candidates.some(item => !item || typeof item.id !== 'string') ||
      new Set(candidates.map(item => item.id)).size !== candidates.length) {
    root.append(node('p', '入口列表不完整或编号重复，请先修正候选。', 'card-note')); return;
  }
  const selected = new Set(Array.isArray(draft.selectedEntryIds) ? draft.selectedEntryIds : candidates.map(item => item.id));
  const summary = node('p', undefined, 'card-note'); root.append(summary);
  const update = () => {
    const ids = candidates.filter(item => selected.has(item.id)).map(item => item.id);
    draft.selectedEntryIds = ids;
    summary.textContent = ids.length ? '保留 ' + ids.length + ' / ' + candidates.length + ' 个入口。' :
      '当前不保留任何入口。安装后，该来源的现有入口将全部移出能力池。';
  };
  const boxes = [];
  for (const entry of candidates) {
    const label = node('label', undefined, 'revision-field');
    const input = node('input'); input.type = 'checkbox'; input.checked = selected.has(entry.id); input.disabled = locked;
    input.setAttribute('aria-label', '保留入口 ' + (entry.description || entry.id));
    input.onchange = () => { if (input.checked) selected.add(entry.id); else selected.delete(entry.id); update(); changed(); };
    label.append(input, node('span', entry.description || entry.capability?.goal || entry.id));
    if (entry.task_role) label.append(node('small', '用途判断：' + taskRoleLabel(entry.task_role) + '（待核对）'));
    root.append(label, node('p', (entry.path || '') + (entry.symbol ? ' · ' + entry.symbol : ''), 'card-meta'));
    boxes.push(input);
  }
  const all = node('button', '全选当前候选', 'secondary-button'); all.type = 'button'; all.disabled = locked;
  all.onclick = () => { for (const entry of candidates) selected.add(entry.id); for (const input of boxes) input.checked = true; update(); changed(); };
  root.append(all, node('p', '移除时请填写下方修改理由。若让模型重新生成候选，请重新确认入口列表。', 'card-note'));
  const previous = draft.selectedEntryIds;
  update();
  if (!Array.isArray(previous)) delete draft.selectedEntryIds;
}

function renderRevisionWorkspace(card, harness, job) {
  const summaries = (state.analysis_revisions || []).filter(item => item.base_analysis_id === job.id);
  const requestedRevision = inspectQuery.get('revision');
  if (!revisionSelected.has(job.id) && summaries.some(item => item.id === requestedRevision)) revisionSelected.set(job.id, requestedRevision);
  const isCurrent = job.id === harness.analysis_id;
  const eligible = isCurrent && job.status === 'COMPLETED' && job.submitted?.candidate;
  if (!eligible && !summaries.length) return;
  const workspace = node('section', undefined, 'revision-workspace');
  workspace.id = 'analysis-revision-' + job.id;
  workspace.append(node('h4', '候选修订工作台'));
  workspace.append(node('p', '编辑、保存和检查只作用于草稿。模型修复后仍对完整候选重检，再独立安装；跨源码版本重新检查全部条件。', 'card-note'));
  if (!isCurrent) workspace.append(node('p', '此分析为只读历史；可回到已创建的修订查看反馈与后续尝试。', 'card-note'));
  if (eligible) {
    if (!revisionCreation.has(job.id)) revisionCreation.set(job.id, {mode: 'refine', revision: ''});
    const creation = revisionCreation.get(job.id);
    const controls = node('div', undefined, 'revision-create');
    const modeLabel = node('label', undefined, 'revision-field'); modeLabel.append(node('span', '修订模式'));
    const mode = node('select');
    for (const [value, label] of [['refine', '同源修复：沿用冻结源码'], ['update', '源码更新：重新冻结当前本地目录或拉取 URL']]) {
      const option = node('option', label); option.value = value; mode.append(option);
    }
    mode.value = creation.mode;
    mode.onchange = () => { creation.mode = mode.value; revisionField.hidden = mode.value !== 'update'; };
    modeLabel.append(mode); controls.append(modeLabel);
    const revision = revisionInput(controls, 'URL revision（可选；本地目录读取当前内容）', creation.revision,
      job.id + ':source', value => { creation.revision = value; });
    const revisionField = revision.parentElement; revisionField.hidden = creation.mode !== 'update';
    const create = node('button', revisionBusy.has(job.id) ? '冻结来源中…' : '创建独立修订', 'secondary-button');
    create.disabled = revisionBusy.has(job.id) || harness.status === 'DETACHED' || harness.status === 'ANALYZING';
    create.onclick = async () => {
      revisionBusy.add(job.id); create.disabled = true; create.textContent = '冻结来源中…';
      revisionErrors.delete(job.id);
      try {
        const body = {analysis_id: job.id, mode: creation.mode, intervention: {actor: {kind: 'user', name: ''}}};
        if (creation.mode === 'update' && creation.revision.trim()) body.revision = creation.revision.trim();
        const result = await api('/api/analysis/revisions', body);
        acceptRevision(result.revision); revisionSelected.set(job.id, result.revision.id);
        showNotice('已创建独立草稿，原分析与当前能力池保留。');
      } catch (error) { revisionErrors.set(job.id, error.message); }
      finally { revisionBusy.delete(job.id); renderedSections.delete('analyses'); await refresh(); }
    };
    controls.append(create); workspace.append(controls);
  }
  if (revisionErrors.has(job.id)) workspace.append(node('p', revisionErrors.get(job.id), 'message-error'));
  if (summaries.length) {
    const selector = node('select'); selector.setAttribute('aria-label', '选择已有修订');
    selector.append(node('option', '选择已有修订…'));
    selector.firstChild.value = '';
    for (const summary of summaries) {
      const option = node('option', summary.id + ' · ' + statusLabel(summary.status) + ' · ' + (summary.updated_at || ''));
      option.value = summary.id; selector.append(option);
    }
    selector.value = revisionSelected.get(job.id) || '';
    selector.onchange = async () => {
      revisionSelected.set(job.id, selector.value);
      renderedSections.delete('analyses'); await refresh();
    };
    workspace.append(selector);
  }
  const id = revisionSelected.get(job.id);
  const revision = revisionDetails.get(id);
  if (id && revisionErrors.has(id)) workspace.append(node('p', revisionErrors.get(id), 'message-error'));
  if (id && !revision) workspace.append(node('p', '正在读取修订…', 'card-note'));
  if (revision) {
    const draft = revisionDrafts.get(id);
    const stale = !isCurrent || revision.stale === true || revision.current_basis_matches === false;
    const locked = revisionBusy.has(id) || stale || harness.status === 'DETACHED' || ['ANALYZING', 'INSTALLED'].includes(revision.status);
    const head = node('div', undefined, 'card-heading'); head.append(node('strong', id), pill(revision.status)); workspace.append(head);
    workspace.append(node('p', '模式：' + (revision.mode === 'update' ? '源码更新' : '同源修复') +
      ' · 父候选摘要：' + (revision.base_candidate_digest || '未提供'), 'card-meta'));
    detailValue(workspace, '父来源版本', revision.base_source_version);
    detailValue(workspace, '本次冻结来源版本', revision.source_version);
    detailValue(workspace, '介入来源、逐次差异与材料记录', revision.provenance);
    detailValue(workspace, '材料复用范围（不沿用旧版本放行结论）', revision.reuse);
    detailValue(workspace, '源码差异与覆盖说明', revision.source_diff, revision.mode === 'update');
    detailValue(workspace, '已保存候选相对父候选的差异', revision.candidate_diff);
    if (revision.status === 'INSTALLED') workspace.append(node('p', '候选已安装，候选内容只读；下方仍可补记介入、个例结果与共性改进。', 'card-note'));
    else if (stale) workspace.append(node('p', (revision.stale_reason || '父分析或来源条件已改变，请从当前分析创建新修订。') + ' 候选内容只读，下方仍可补记处理记录。', 'card-note'));
    if (draft.dirty) detailValue(workspace, '服务器已保存的完整候选（本地未保存编辑另行保留）', revision.candidate);
    const entrySelection = node('section', undefined, 'revision-entry-selection');
    renderRevisionEntrySelection(entrySelection, draft, locked, changed); workspace.append(entrySelection);
    const advanced = node('details'); advanced.append(node('summary', '高级：编辑完整候选 JSON'));
    const editor = revisionInput(advanced, '完整候选 JSON（人工编辑）', draft.candidate, id + ':candidate', value => {
      draft.candidate = value; renderRevisionEntrySelection(entrySelection, draft, locked, changed); changed();
    }, true);
    editor.classList.add('revision-candidate'); editor.spellcheck = false;
    workspace.append(advanced);
    const guidance = revisionInput(workspace, '局部修复提示／关注范围', draft.guidance, id + ':guidance', value => {
      draft.guidance = value; changed();
    }, true);
    const reason = revisionInput(workspace, '修改理由', draft.reason, id + ':reason', value => {
      draft.reason = value; changed();
    }, true);
    for (const input of [editor, guidance, reason]) input.disabled = locked;
    const feedback = node('p', undefined, 'card-note'); workspace.append(feedback);
    const actions = node('div', undefined, 'harness-actions');
    const buttons = {};
    for (const [operation, label] of [['save', '保存草稿'], ['check', '保存并完整重检'], ['refine', '调用当前分析模型局部修复'], ['install', '独立安装新候选']]) {
      const button = node('button', label, 'secondary-button'); button.type = 'button';
      button.onclick = () => revisionAction(id, operation); buttons[operation] = button; actions.append(button);
    }
    function updateControls() {
      const checked = !draft.dirty && revision.report && revision.checked_digest && revision.checked_digest === revision.candidate_digest;
      feedback.textContent = draft.dirty ? '有未保存编辑，差异与检查报告对应上次保存内容；请保存或完整重检。' :
        checked ? '完整检查已对应当前候选；仍以逐项反馈和安装时环境核对为准。' : '当前候选尚无匹配的完整检查，请先完整重检。';
      for (const button of Object.values(buttons)) button.disabled = locked || harness.status === 'DETACHED';
      buttons.refine.disabled ||= !isReady(state.models?.analysis);
      buttons.install.disabled ||= !checked || harness.status === 'ANALYZING';
    }
    function changed() { draft.dirty = true; updateControls(); }
    updateControls(); workspace.append(actions);
    workspace.append(node('p', '局部修复会调用当前配置的分析模型：' + (state.models?.analysis?.model || '尚未配置') +
      '。保存和机械重检不调用模型。', 'card-note'));
    if (revision.error) workspace.append(node('p', textValue(revision.error), 'message-error'));
    detailValue(workspace, '当前完整检查报告（逐项条件与失败原因）', revision.report);
    const attempt = (state.analyses || []).find(item => item.id === revision.analysis_id);
    if (attempt) {
      workspace.append(node('p', '本次模型尝试：' + attempt.id + ' · ' + statusLabel(attempt.status), 'card-meta'), analysisProgress(attempt));
      if (attempt.error) workspace.append(node('p', textValue(attempt.error), 'message-error'));
      detailValue(workspace, '本次模型与实际请求参数', attempt.model_binding);
    }
    detailValue(workspace, '本次用量记录', attempt?.usage ?? revision.usage);
    if (revision.installed_analysis_id) workspace.append(node('p', '安装后的分析身份：' + revision.installed_analysis_id, 'card-meta'));
    renderRevisionFeedback(workspace, revision);
  }
  card.append(workspace);
}

function issueText(issue) {
  if (typeof issue === 'string') return ({
    'configurable dependency needs original parameter mappings': '已发现资源需求，但尚未建立到原代码参数的有效映射',
    'required source resource mapping is unresolved': '必需资源与原代码参数的映射未解决',
    'no functional target was discovered': '尚未识别到原功能目标',
    'no native entry candidate was discovered': '尚未找到原生入口候选'
  })[issue] || issue;
  if (!issue || typeof issue !== 'object') return '条件尚未确定';
  return [issue.path || issue.id || issue.field, issue.reason || issue.description || issue.detail || issue.message || issue.condition]
    .filter(value => typeof value === 'string').map(issueText).join(' · ') || '查看原始记录中的条件说明';
}

// Full model transcripts are read only on request, never by the status poll.
const analysisTranscripts = new Map();

function analysisTranscript(kind, id, modelCalls) {
  const root = node('details', undefined, 'analysis-transcript');
  root.append(node('summary', (kind === 'preprocessing' ? '预处理' : '拆分分析') + '实际模型输入与过程'));
  const button = node('button', '读取已保存消息', 'secondary-button'); button.type = 'button';
  const content = node('div');
  const key = kind + ':' + id;
  const render = () => {
    content.replaceChildren();
    const record = analysisTranscripts.get(key);
    if (!record) return;
    content.append(node('p', '按保存顺序展示；此为手动读取的快照，可重新读取。工具回复对应 tool_call_id；模型提出的工具调用不等于工具已成功执行。', 'card-note'));
    if (!Array.isArray(record.messages)) {
      content.append(node('p', '此记录未保存 messages，无法展示模型历史。', 'card-note')); return;
    }
    if (!record.messages.length) {
      content.append(node('p', '本记录 messages=[]，没有已保存的模型消息。' +
        (record.model_calls === 0 || modelCalls === 0 ? '本次未调用模型。' : '仅凭空消息无法确认模型调用情况。') +
        '不补造原分析历史。', 'card-note')); return;
    }
    record.messages.forEach((message, index) => {
      const item = node('details');
      const role = message.role || '未记录角色';
      item.append(node('summary', (index + 1) + '. ' + role +
        (message.name ? ' · ' + message.name : '') + (message.tool_call_id ? ' · ' + message.tool_call_id : '')));
      let filled = false;
      item.ontoggle = () => {
        if (!item.open || filled) return;
        filled = true;
        if (message.content !== undefined && message.content !== null) item.append(node('pre', textValue(message.content)));
        if (Array.isArray(message.tool_calls)) message.tool_calls.forEach((call, callIndex) => {
          item.append(node('p', '工具调用 ' + (callIndex + 1) + ' · ' + (call.id || '') + ' · ' +
            (call.function?.name || call.name || '未记录名称'), 'card-meta'),
          node('pre', textValue(call.function?.arguments ?? call.arguments ?? {})));
        });
        if (message.function_call) {
          item.append(node('p', '工具调用 · ' + (message.function_call.name || ''), 'card-meta'),
            node('pre', textValue(message.function_call.arguments)));
        }
        if (message.content == null && !message.tool_calls?.length && !message.function_call)
          item.append(node('p', '此条未保存内容或工具调用。', 'card-note'));
      };
      content.append(item);
    });
  };
  root.ontoggle = () => { if (root.open && !content.childNodes.length) render(); };
  button.onclick = async () => {
    button.disabled = true;
    try {
      const record = await api('/api/' + (kind === 'preprocessing' ? 'preprocessing' : 'analysis') + '/' + encodeURIComponent(id));
      analysisTranscripts.set(key, {messages: record.messages, model_calls: record.model_calls});
      render(); button.textContent = '重新读取已保存消息';
    } catch (error) { content.replaceChildren(node('p', '读取失败：' + error.message, 'card-note')); }
    finally { button.disabled = false; }
  };
  if (analysisTranscripts.has(key)) button.textContent = '重新读取已保存消息';
  root.append(button, content);
  return root;
}

function analysisClassification(classification) {
  if (!classification || classification.schema !== 'harness-classification/1') return null;
  const root = node('section', undefined, 'analysis-classification');
  root.append(node('h4', '源码分类与调查配方'), node('p',
    '分类是源码调查线索，允许多标签与未知；不表示初始化、执行或权限检查已通过。', 'card-note'));
  detailValue(root, '本次分类覆盖范围', classification.scope);
  if (classification.next_offset != null) root.append(node('p',
    '还有未扫描的文件；后续调查可从文件偏移 ' + classification.next_offset + ' 继续。', 'card-note'));
  const profiles = Array.isArray(classification.profiles) ? classification.profiles : [];
  if (!profiles.length) root.append(node('p', '本记录尚无分类条目。', 'card-note'));
  for (const profile of profiles) {
    const item = node('details');
    item.append(node('summary', [profile.path, profile.unit, profile.symbol, profile.kind].filter(Boolean).join(' · ') || '未指定源码范围'));
    detailValue(item, '条目范围', profile.scope);
    if (profile.task_role) detailValue(item, '用途判断（分析声明）', profile.task_role);
    for (const [field, label] of [['forms', '形态'], ['families', '功能类型'], ['traits', '特征']]) {
      const tags = Array.isArray(profile[field]) ? profile[field] : [];
      if (!tags.length) item.append(node('p', label + '：未知／未提供', 'card-note'));
      for (const tag of tags) {
        const evidence = node('details');
        const status = {SOURCE_FACT: '语法事实', SOURCE_SIGNAL: '源码线索', HYPOTHESIS: '待核对假设', UNKNOWN: '未知'};
        evidence.append(node('summary', label + '：' + (tag.label || tag.id || '未知') + ' · ' + (status[tag.status] || tag.status || '未标状态')),
          node('pre', tag.evidence == null ? '未提供源码证据' : textValue(tag.evidence)));
        if (tag.evidence_truncated) evidence.append(node('p',
          '共 ' + tag.evidence_count + ' 处线索，预览前 ' + (tag.evidence?.length || 0) + ' 处；其余需继续读取对应源码。', 'card-note'));
        item.append(evidence);
      }
    }
    for (const recipe of Array.isArray(profile.recipes) ? profile.recipes : []) {
      const detail = node('details'); detail.append(node('summary', '调查配方：' + (recipe.title || recipe.id || '未命名')));
      if (recipe.reason) detail.append(node('p', recipe.reason, 'card-note'));
      const steps = node('ol');
      for (const step of Array.isArray(recipe.steps) ? recipe.steps : []) {
        const line = node('li'); line.append(node('strong', step.tool || '未指定工具'));
        if (step.purpose) line.append(node('p', step.purpose, 'card-note'));
        detailValue(line, '调查参数', step.arguments); steps.append(line);
      }
      detail.append(steps); item.append(detail);
    }
    detailValue(item, '尚未确定的条件', profile.unknowns);
    root.append(item);
  }
  detailValue(root, '分类整体未知项', classification.unknowns);
  return root;
}

function taskRoleLabel(claim) {
  return {target_defense: '目标防护', evidence: '检索或补证', host: '任务或对话宿主', support: '必要支撑',
    display: '界面展示', mock: '测试替身', offline: '离线训练或评价', unknown: '尚未确定'}[claim?.role] || '尚未确定';
}

function entryIssueSummary(issue) {
  const specific = {
    candidate_incomplete: ['分析结果缺少必要信息', '需要分析器补齐原参数、入口或资源描述，再完整检查。'],
    evidence_incomplete: ['源码依据还不完整', '需要分析器补读原定义、赋值和完整调用，检查记录中列出了缺少的位置。'],
    parameter_flow_unsupported: ['平台暂不支持这段参数传递', '需要补充平台的通用检查规则；重复添加资源不能解决。'],
    resource_locator_unsupported: ['模型来源还没核对清楚', '需要核对原模型标识、配置分支和缓存；不能把缓存目录当作模型文件。'],
    initialization_incomplete: ['没有保留完整的初始化步骤', '需要按原代码保留权重加载、设备和模式设置；支持不了时继续记录缺口。']
  };
  if (specific[issue.issue_code]) {
    const [title, action] = specific[issue.issue_code]; return {title, action};
  }
  const summaries = {
    parameter_mapping: ['这项功能还没接好', '需要分析器继续修正接线；如果修正后仍是同一问题，需要改进平台。现在不用重复添加资源。'],
    resource_binding: ['还没有选好所需资源', '请选择对应的服务或模型文件，再重新检查。'],
    source_anchor: ['分析结果与原代码位置对不上', '需要分析器重新查找原函数，再检查接入结果。'],
    unsupported_rule: ['平台暂不支持这段代码的接入方式', '需要改进平台；添加服务或模型文件不能解决这个问题。'],
    runtime_environment: ['运行条件还没准备好', '请检查运行环境、依赖包和文件是否齐全，再重新检查。'],
    binding_unresolved: ['这项功能还没接好', '需要继续检查接入方式；具体原因保留在下方原始记录中。']
  };
  const [title, action] = summaries[issue.category] || ['接入检查尚未通过', '请查看原始记录，确认还需要补齐什么。'];
  return {title, action};
}

function entryReadinessDetails(readiness, job) {
  const root = node('div', undefined, 'entry-readiness');
  root.append(node('p', readiness.status === 'STATIC_READY' ? '接入检查已通过，还需要实际运行验证。' :
    '接入检查尚未通过，请先处理下方问题。', 'card-note'));
  root.append(node('p', '本次检查没有运行原功能；已有结果请查看运行记录。', 'card-note'));
  if (readiness.task_role?.status && readiness.task_role.status !== 'NOT_DECLARED') {
    root.append(node('p', '用途判断：' + taskRoleLabel(readiness.task_role) + '（待核对）', 'card-note'));
  }
  for (const issue of Array.isArray(readiness.issues) ? readiness.issues : []) {
    const summary = entryIssueSummary(issue);
    root.append(node('p', summary.title, 'card-note'));
    root.append(node('p', '下一步：' + summary.action, 'card-note'));
    if (inspectPage) {
      const section = {resources: 'harnesses', services: 'services', weights: 'weights', environment: 'harnesses'}[issue.target];
      if (section || issue.target === 'revision') {
        const link = node('a', section ? '打开对应配置' : '查看分析与修正', 'detail-link');
        link.href = section ? '/configuration?section=' + section : '#analysis-revision-' + job.id;
        root.append(link);
      }
    }
  }
  detailValue(root, '查看原始检查记录', {status: readiness.status, reason: readiness.reason,
    initialization: readiness.initialization, execution: readiness.execution, scope: readiness.scope,
    next_step: readiness.next_step, issues: readiness.issues, task_role: readiness.task_role});
  detailValue(root, '入口参数', readiness.parameters);
  detailValue(root, '入口操作与绑定条件', readiness.operations);
  return root;
}

async function refreshPreparationSummaries() {
  if (!inspectPage || inspectView !== 'analyses') return;
  const jobs = (state.analyses || []).filter(job => !selectedSource() || job.harness_id === selectedSource());
  const ids = [...new Set(jobs.map(job => job.preprocessing?.artifact_id).filter(Boolean))];
  await Promise.allSettled(ids.filter(id => !preparationSummaries.has(id) && !preparationErrors.has(id)).map(async id => {
    try { preparationSummaries.set(id, await api('/api/preprocessing/' + encodeURIComponent(id) + '/summary')); }
    catch (error) { preparationErrors.set(id, error.message); }
  }));
}

function preprocessingSummary(job) {
  const prep = job.preprocessing;
  if (!prep) return null;
  const root = node('section', undefined, 'preprocessing-summary');
  root.append(node('h4', '预处理：先了解仓库'));
  root.append(node('p', '分析器先查找原功能入口、初始化方式和外部依赖，再把这些发现交给后续拆分分析。保存的“工件”就是这份中间结果。', 'card-note'));
  root.append(node('p', ({api: '本次自动分析源码', replay: '复用已保存的发现', none: '本次跳过预处理'}[prep.mode] || prep.mode) +
    ' · ' + statusLabel(prep.status) + '；本阶段不运行原功能，也不接入能力池。', 'card-note'));
  const summary = preparationSummaries.get(prep.artifact_id);
  if (summary) {
    const classification = analysisClassification(summary.classification);
    if (classification) root.append(classification);
    if (summary.goal) root.append(node('p', '识别目标（分析器描述）：' + summary.goal, 'card-note'));
    root.append(node('p', '发现 ' + summary.targets.length + ' 个功能位置、提出 ' + summary.entries.length +
      ' 个入口候选、记录 ' + summary.dependency_count + ' 项外部依赖。候选会在后续阶段继续检查和调整。', 'card-note'));
    const detail = node('details'); detail.append(node('summary', '发现的原功能与候选入口'));
    const table = node('table', undefined, 'research-metrics');
    const head = node('tr');
    for (const text of ['原功能（分析器描述）', '源码位置', '用途类别']) head.append(node('th', text));
    const thead = node('thead'); thead.append(head); table.append(thead);
    const body = node('tbody');
    const roles = {online: '在线调用', offline: '离线处理／评测', initialization: '初始化', cleanup: '清理', mixed: '混合', unknown: '未确定'};
    for (const target of summary.targets) {
      const row = node('tr'); row.append(node('td', target.purpose || target.symbol),
        node('td', (target.path || '') + ' · ' + (target.symbol || '')), node('td', roles[target.execution_role] || '未确定')); body.append(row);
    }
    table.append(body); detail.append(table);
    for (const entry of summary.entries) detail.append(node('p', entry.symbol + ' · ' +
      (entry.status === 'READY' ? '预处理时静态可绑定' : '预处理时仍缺条件') +
      (entry.reason ? '：' + issueText(entry.reason) : ''), 'card-note'));
    for (const readiness of summary.entry_readiness || []) {
      const entry = node('details'); entry.append(node('summary', '预处理入口准备 · ' + readiness.entry_id),
        entryReadinessDetails(readiness, job)); detail.append(entry);
    }
    root.append(detail);
    const issues = [...summary.missing, ...summary.issues];
    if (issues.length) {
      const gaps = node('details'); gaps.append(node('summary', '预处理时未解决的条件 · ' + issues.length));
      const list = node('ul'); for (const issue of issues) list.append(node('li', issueText(issue)));
      gaps.append(list, node('p', '这些是当时的发现；当前接入状态请看分析结论和下方补齐指引。', 'card-note')); root.append(gaps);
    }
  } else if (preparationErrors.has(prep.artifact_id)) {
    root.append(node('p', '摘要暂时无法读取：' + preparationErrors.get(prep.artifact_id), 'card-note'));
    const retry = node('button', '重新读取摘要', 'secondary-button'); retry.onclick = async () => {
      preparationErrors.delete(prep.artifact_id); await refresh();
    }; root.append(retry);
  }
  if (inspectPage) {
    const original = node('details'); original.append(node('summary', '预处理技术记录'));
    if (prep.artifact_id) {
      original.append(analysisTranscript('preprocessing', prep.artifact_id));
      original.append(node('p', '记录编号：' + prep.artifact_id, 'card-meta'));
      const link = node('a', '查看完整预处理 JSON'); link.href = '/api/preprocessing/' + encodeURIComponent(prep.artifact_id);
      link.target = '_blank'; original.append(link);
    }
    detailValue(original, '原始检查反馈', prep.report); root.append(original);
  }
  return root;
}

async function prepareRepair(harness, job) {
  if (revisionBusy.has(job.id)) return;
  revisionBusy.add(job.id);
  try {
    const created = await api('/api/analysis/revisions', {analysis_id: job.id, mode: 'refine', intervention: {actor: {kind: 'user', name: ''}}});
    const guidance = '请沿已发现的原目标检查未解决的依赖、原参数映射及受控边界。补充原源码证据和现有规则支持的映射；不能通过无关辅助入口、假资源或手写来源包装消除缺口。不能覆盖的条件保留具体 MISSING。';
    const saved = await api('/api/analysis/revisions/save', {revision_id: created.revision.id,
      candidate: created.revision.candidate, guidance, reason: '补齐当前来源的接入缺口', intervention: {actor: {kind: 'user', name: ''}}});
    acceptRevision(saved.revision); revisionSelected.set(job.id, saved.revision.id);
    showNotice('修复草稿和缺口提示已准备。请在下方工作台补充信息，再点击“调用当前分析模型局部修复”；修复后完整重检并独立安装。');
  } catch (error) { showNotice(error.message); }
  finally { revisionBusy.delete(job.id); renderedSections.delete('analyses'); await refresh(); }
}

function analysisNextSteps(harness, job) {
  if (job?.id !== harness.analysis_id || job?.status !== 'COMPLETED' || !job.submitted?.candidate) return null;
  const root = node('section', undefined, 'analysis-next-steps'); root.append(node('h4', '补齐条件与接入'));
  const list = node('ol');
  list.append(node('li', '缺服务或权重：先在配置中登记并检查资源，再在下方“依赖配置”选择对应资源。保存选择会重新核对并接入满足条件的入口，不再调用分析模型。'),
    node('li', '缺参数映射、源码依据或宿主支持：先修复候选；仅选择资源不能解决。可创建带缺口提示的修复草稿，用分析模型修复后完整重检，再独立安装。宿主规则未覆盖时须修通用实现，不能强行接入。'),
    node('li', '文件位置或运行环境已补齐：重新检查现有候选并接入。成功后到能力池查看具体入口；静态可绑定之后仍需实际调用核对。'));
  root.append(list);
  const actions = node('div', undefined, 'harness-actions');
  for (const [section, text] of [['services', '配置服务连接'], ['weights', '配置权重资源'], ['harnesses', '配置运行环境']]) {
    const link = node('a', text, 'secondary-button'); link.href = '/configuration?section=' + section; actions.append(link);
  }
  const repair = node('button', '创建接入修复草稿', 'secondary-button'); repair.type = 'button';
  repair.disabled = revisionBusy.has(job.id) || ['DETACHED', 'ANALYZING'].includes(harness.status); repair.onclick = () => prepareRepair(harness, job);
  const recheck = node('button', '重新检查并接入可用入口', 'secondary-button'); recheck.type = 'button';
  recheck.disabled = revisionBusy.has(job.id) || ['DETACHED', 'ANALYZING'].includes(harness.status);
  recheck.onclick = async () => {
    if (revisionBusy.has(job.id)) return;
    revisionBusy.add(job.id); recheck.disabled = true; recheck.textContent = '正在完整检查…';
    try {
      const created = await api('/api/analysis/revisions', {analysis_id: job.id, mode: 'refine', intervention: {actor: {kind: 'user', name: ''}}});
      revisionSelected.set(job.id, created.revision.id);
      const checked = await api('/api/analysis/revisions/check', {revision_id: created.revision.id, intervention: {actor: {kind: 'user', name: ''}}});
      acceptRevision(checked.revision);
      if (checked.revision.status !== 'CHECKED') throw new Error(checked.revision.error || '候选检查未完成，请查看修订反馈');
      await api('/api/analysis/revisions/install', {revision_id: created.revision.id, intervention: {actor: {kind: 'user', name: ''}}});
      showNotice('已按当前资源重新检查并安装；满足条件的入口可在能力池查看，仍缺条件的入口继续显示待接入。本次未调用分析模型。');
    } catch (error) { showNotice(error.message); }
    finally { revisionBusy.delete(job.id); renderedSections.delete('analyses'); await refresh(); }
  };
  const pool = inspectLink('units', {harness: harness.id}); pool.textContent = '查看该来源的能力池';
  actions.append(repair, recheck, pool); root.append(actions);
  return root;
}

function analysisResultSummary(job) {
  const candidate = job.submitted?.candidate;
  const report = job.submitted?.report;
  const bindings = report?.entry_bindings;
  if (!candidate || !bindings || !Array.isArray(bindings.entries)) return null;
  const root = node('section', undefined, 'analysis-result-summary');
  root.append(node('h4', '本次分析结论'));
  const ready = bindings.entries.filter(entry => entry.status === 'READY').length;
  const missing = bindings.entries.filter(entry => entry.status === 'MISSING').length;
  root.append(node('p', '发现 ' + bindings.entries.length + ' 个入口：' + ready + ' 个静态可绑定，' + missing +
    ' 个缺少执行条件。' + (['COMPLETED', 'FAILED', 'CANCELLED', 'INTERRUPTED'].includes(job.status) ?
      '分析任务已结束。' : '分析仍在进行。'), 'card-note'));
  root.append(node('p', '静态可绑定表示源码与绑定条件已核对；实际初始化、执行结果和任务效果另见任务记录。', 'card-note'));
  const entries = new Map((candidate.entries || []).map(entry => [entry.id, entry]));
  const reasons = {'required source resource mapping is unresolved': '必需资源与原参数的映射未解决',
    'no bindable native entry was discovered': '尚未找到可绑定的原生入口'};
  const table = node('table', undefined, 'research-metrics analysis-usage');
  const header = node('tr');
  for (const title of ['入口与用途（分析器描述）', '静态绑定', '当前条件']) header.append(node('th', title));
  const thead = node('thead'); thead.append(header); table.append(thead);
  const body = node('tbody');
  for (const result of bindings.entries) {
    const entry = entries.get(result.id) || {};
    const cell = node('td');
    const title = entry.capability?.goal || entry.description || result.id;
    cell.append(node('p', String(title).split(/[。\n]/)[0]), node('code', (result.path || entry.path || '') +
      ((result.symbol || entry.symbol) ? ' · ' + (result.symbol || entry.symbol) : '')));
    const row = node('tr');
    const conditions = node('td', result.reason ? reasons[result.reason] || result.reason : bindings.initialization === 'NOT_RUN' ? '本报告未执行初始化' : '查看具体检查结果');
    const readiness = (Array.isArray(report.entry_readiness) ? report.entry_readiness : []).find(item => item.entry_id === result.id);
    if (readiness) conditions.append(entryReadinessDetails(readiness, job));
    row.append(cell, node('td', {READY: '静态可绑定', MISSING: '缺少条件'}[result.status] || statusLabel(result.status)), conditions);
    body.append(row);
  }
  table.append(body); root.append(table);
  const modes = {W: '整体包装', H: '原生交接', R: '有限局部重构'};
  const boundaries = Array.isArray(candidate.boundaries) ? candidate.boundaries : [];
  if (boundaries.length) {
    const counts = {};
    for (const boundary of boundaries) counts[boundary.mode] = (counts[boundary.mode] || 0) + 1;
    root.append(node('p', '受控边界：' + Object.entries(counts).map(([mode, count]) => count + ' 处' +
      (modes[mode] || mode) + '（' + mode + '）').join('；') + '。边界数不等于可独立调用的入口数。', 'card-note'));
  }
  if (!boundaries.length) root.append(node('p', '本次候选未提出受控边界；入口识别结果不代表已完成有效拆分。', 'card-note'));
  const grouping = bindings.runtime_groups;
  if (Array.isArray(grouping?.groups)) {
    root.append(node('p', '模型建议 ' + (candidate.runtime_groups || []).length + ' 个运行组，规则检查后为 ' +
      grouping.groups.length + ' 个。' + (grouping.proposal_status === 'REPAIRED_CONSERVATIVELY' ?
        '分组建议存在未决条件，检查器按当前规则保守归组。' : ''), 'card-note'));
    for (const group of grouping.groups) root.append(node('p', (group.same_process ? '保留同一进程' : '按检查后的组运行') +
      ' · ' + statusLabel(group.status) + ' · ' + (group.entries || []).length + ' 个入口', 'card-meta'));
  }
  return root;
}

function analysisOperationLabel(job) {
  if (!job) return '尚未分析';
  if (job.resumed_from) return '继续未完成的分析';
  const named = {
    ENVIRONMENT_RECHECK: '按当前环境重新核对已有候选',
    CANDIDATE_REVISION_INSTALL: '安装修订后的候选',
    EXPLICIT_RESOURCE_BINDING: '保存显式配置并重新接入',
    ANALYZER_RESOURCE_BINDING: '保存依赖选择并重新接入',
    IMPORTED_ANALYSIS: '导入已有分析',
    CANDIDATE_REFINEMENT: '用分析模型修订候选'
  };
  if (named[job.kind]) return named[job.kind];
  if (job.refinement_of) return '沿用已有分析继续完善';
  if ((job.rounds || job.model_calls || 0) > 0) return '分析源码并形成接入结果';
  return '检查并安装接入结果';
}

function sourceIdentity(harness) {
  const block = node('div', undefined, 'analysis-source-heading');
  block.append(node('h3', harness.name || harness.address || '未命名来源'));
  const remote = typeof harness.address === 'string' && harness.address.includes('://');
  if (harness.address) block.append(node('p', (remote ? '远端地址：' : '来源地址：') + harness.address, 'card-meta'));
  if (harness.root) block.append(node('p', '本地位置：' + harness.root, 'resource-path'));
  const files = entryCount(harness.files);
  if (files !== null) block.append(node('p', files + ' 份可读文件' +
    (typeof harness.inventory_count === 'number' ? ' · 仓库共 ' + harness.inventory_count + ' 项文件' : ''), 'card-meta'));
  return block;
}

function renderAnalyses() {
  const harnesses = (state.harnesses || []).filter(source => !selectedSource() || source.id === selectedSource());
  const analyses = state.analyses || [];
  const root = $('analysis-list');
  const heading = $('analysis-heading');
  root.replaceChildren();
  if (heading) heading.replaceChildren(inspectPage && harnesses.length === 1 ? sourceIdentity(harnesses[0]) : node('h3', '来源与分析'));
  $('analysis-count').textContent = analyses.filter(job => harnesses.some(source => source.id === job.harness_id)).length;
  if (!harnesses.length) return empty(root, '配置 Harness 地址后可开始分析。');
  for (const harness of harnesses) {
    const history = analyses.filter(item => item.harness_id === harness.id);
    const jobs = inspectPage ? history.length ? history : [null] :
      [analyses.find(item => item.id === harness.analysis_id) || history[0] || null];
    const steps = new Map([...history].reverse().map((job, index) => [job.id, index + 1]));
    if (inspectPage && harnesses.length !== 1) root.append(sourceIdentity(harness));
    for (const job of jobs) {
    const card = node('article', undefined, 'status-card');
    card.dataset.itemId = harness.id + ':' + (job?.id || 'source');
    const head = node('div', undefined, 'card-heading');
    const title = inspectPage ? (job ? '第' + steps.get(job.id) + '次操作：' + analysisOperationLabel(job) : '尚未分析') :
      (harness.name || harness.address);
    head.append(node('strong', title), pill(harness.status === 'DETACHED' ? 'DETACHED' : job?.status || harness.status));
    card.append(head);
    if (!inspectPage) {
      card.append(node('p', harness.address, 'card-meta'));
      if ((!job || job.id === harness.analysis_id) && harness.root) card.append(node('p', '当前本地源码目录：' + harness.root, 'resource-path'));
      const files = entryCount(harness.files);
      if (files !== null) card.append(node('p', files + ' 份可读文件' + (typeof harness.inventory_count === 'number' ? ' · 仓库共 ' + harness.inventory_count + ' 项文件' : ''), 'card-meta'));
    }
    if (job?.rounds !== undefined) card.append(node('p', '分析模型响应 ' + job.rounds + ' 轮', 'card-meta'));
    if (job) card.append(analysisProgress(job));
    const outcome = job?.outcome || job?.submitted?.report?.outcome;
    const outcomeText = {NEEDS_CANDIDATE_REPAIR: '分析已结束，候选或源码证据还需修正。',
      AWAITING_EXTERNAL_CONDITIONS: '分析已结束，仍有平台支持、资源或运行条件待补齐。',
      STATIC_CHECKED_NOT_RUN: '静态检查已通过，尚未实际运行目标。'}[outcome];
    if (outcomeText) card.append(node('p', outcomeText, 'card-note'));
    const classification = analysisClassification(job?.submitted?.report?.classification);
    if (classification) card.append(classification);
    const resultSummary = job && analysisResultSummary(job);
    if (resultSummary) card.append(resultSummary);
    if (!resultSummary && Array.isArray(job?.submitted?.report?.entry_readiness)) {
      for (const readiness of job.submitted.report.entry_readiness) {
        const entry = node('section'); entry.append(node('h4', '入口 ' + readiness.entry_id), entryReadinessDetails(readiness, job)); card.append(entry);
      }
    }
    const preparation = job && preprocessingSummary(job);
    if (preparation) card.append(preparation);
    if (inspectPage) {
      const sourceVersion = job?.submitted?.report?.source_version || job?.source_version;
      card.append(node('p', '本记录冻结源码版本：' + (sourceVersion || '未提供；以原分析工件为准'), 'card-meta'));
      if (!job || job.id === harness.analysis_id) card.append(node('p', '来源 revision：' + (harness.revision || '本地内容版本'), 'card-meta'));
      if (job) {
        card.append(analysisTranscript('analysis', job.id, job.model_calls));
        const originalAnalysis = job.refinement_of || job.resumed_from;
        if (originalAnalysis) {
          const link = node('a', '读取原分析记录（本次消息不替代原历史）', 'detail-link');
          link.href = '/api/analysis/' + encodeURIComponent(originalAnalysis); link.target = '_blank'; card.append(link);
        }
        card.append(node('p', '分析 ' + job.id + ' · ' + statusLabel(job.status) +
          (job.id === harness.analysis_id ? ' · 当前记录' : ' · 历史记录'), 'card-meta'));
        if (job.started_at) card.append(node('p', '开始：' + job.started_at, 'card-meta'));
        if (job.refinement_of) card.append(node('p', '沿用分析上下文：' + job.refinement_of, 'card-meta'));
        if (job.resumed_from) card.append(node('p', '继续分析：' + job.resumed_from +
          ' · 已保留此前 ' + job.prior_rounds + ' 轮，本记录只统计新增用量。', 'card-meta'));
        detailValue(card, '实际模型用量', job.usage);
        if (job.model_binding) detailValue(card, '本记录冻结的模型与请求参数', job.model_binding);
      }
    }
    if (harness.status === 'DETACHED') card.append(node('p', '来源已退出当前能力池。源码和分析历史保留，可重新接入。', 'card-note'));
    const error = job?.error || harness.error;
    if (error) card.append(node('p', textValue(error), 'card-note message-error'));
    if (job?.unreported_usage || job?.resumable) card.append(node('p', '中断请求未返回用量，未将其记作零费用。', 'card-note'));
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
    if (inspectPage && (!job || job.id === harness.analysis_id)) {
      const next = analysisNextSteps(harness, job);
      if (next) card.append(next);
      harnessDependencies(card, harness);
    }
    const actions = node('div', undefined, 'harness-actions');
    if (!inspectPage) actions.append(inspectLink('analyses', {harness: harness.id}));
    if (job?.id) actions.append(analysisExportButton(job));
    const cancel = cancelAnalysisButton(harness, job);
    if (cancel) actions.append(cancel);
    if (job?.status === 'COMPLETED' && job.submitted?.candidate) actions.append(analysisExportButton(job, true));
    if (job?.resumable) {
      const resume = node('button', '继续这次分析', 'secondary-button');
      resume.disabled = harness.status === 'DETACHED' || harness.status === 'ANALYZING' || analysisSubmitting.has(harness.id);
      if (harness.status === 'DETACHED') resume.title = '先重新接入来源，再继续保存的分析；使用当前分析模型配置。';
      resume.onclick = async () => {
        if (analysisSubmitting.has(harness.id)) return;
        analysisSubmitting.add(harness.id); resume.disabled = true;
        try {
          await api('/api/analysis/resume', {analysis_id: job.id});
          showNotice('已保留此前的完整上下文，使用当前分析模型继续；本次用量单独记录。');
        } catch (error) { showNotice(error.message); }
        finally {
          analysisSubmitting.delete(harness.id);
          resume.disabled = harness.status === 'DETACHED' || harness.status === 'ANALYZING';
          await refresh();
        }
      };
      actions.append(resume);
    }
    actions.append(sourceButton(harness), deleteSourceButton(harness));
    card.append(actions);
    if (inspectPage && job) renderRevisionWorkspace(card, harness, job);
    root.append(card);
    }
  }
  if (state.rules?.theory_pending) root.append(node('p', state.rules.theory_pending === true ?
    '分析准则待确认。' : textValue(state.rules.theory_pending), 'theory-note'));
}

function researchState() { return state?.scheduler?.research || {}; }
function runIdentity(run) { return run.run_id || run.id; }
function shortHash(value) {
  if (!value) return '—';
  const text = String(value), split = text.indexOf('_');
  if (split < 0) return text.slice(0, 14);
  const hash = text.slice(split + 1), identities = (researchState().runs || []).map(runIdentity);
  let length = 8;
  while (length < hash.length && identities.some(id => id !== text && id.split('_').at(-1).startsWith(hash.slice(0, length)))) length += 4;
  return text.slice(0, split) + ' · ' + hash.slice(0, length);
}
function finiteNumber(value) { return typeof value === 'number' && Number.isFinite(value); }
function displayNumber(value) { return finiteNumber(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits: 4}) : 'UNKNOWN'; }

function jsonField(id, label, optional = false) {
  const text = $(id).value.trim();
  if (!text && optional) return undefined;
  let result;
  try { result = JSON.parse(text); }
  catch (error) { throw new Error(label + '：' + error.message); }
  if (!result || typeof result !== 'object' || Array.isArray(result)) throw new Error(label + '需要 JSON 对象');
  return result;
}

function blankResearchConfig() {
  return {algorithm: 'double_dqn', parameters: {}, seed: 0, mode: 'train', parent_run_id: null,
    episodes: 1, split: 'development', tags: [], scene: {id: '', version: '1',
      adapter: {path: '', symbol: '', dependencies: [], config: {}},
      observation_schema: [], candidate_schema: [], unit_ids: [], tasks: []},
    criteria: {id: '', version: '1', prompt: '', evidence_fields: [], dimensions: [],
      teacher: {role: 'teacher'}, objective: {weights: {}, risk_limits: {}}}};
}

function loadResearchConfig(config) {
  researchDraft = structuredClone(config || blankResearchConfig());
  const algorithms = researchDraft.algorithms || [researchDraft.algorithm || 'double_dqn'];
  for (const input of document.querySelectorAll('#research-form input[name="algorithm"]')) input.checked = algorithms.includes(input.value);
  $('research-mode').value = researchDraft.mode || 'train';
  $('research-seed').value = researchDraft.seed ?? 0;
  $('research-episodes').value = researchDraft.episodes ?? 1;
  $('research-split').value = researchDraft.split || 'development';
  $('research-tags').value = (researchDraft.tags || []).join(', ');
  $('research-task-ids').value = (researchDraft.task_ids || []).join(', ');
  $('research-scene').value = JSON.stringify(researchDraft.scene || blankResearchConfig().scene, null, 2);
  const criteria = researchDraft.criteria || blankResearchConfig().criteria;
  $('research-criteria').value = JSON.stringify(criteria, null, 2);
  $('research-prompt').value = criteria.prompt || '';
  $('research-teacher-role').value = criteria.teacher?.role || 'teacher';
  $('research-independent').value = researchDraft.evaluation_criteria ? JSON.stringify(researchDraft.evaluation_criteria, null, 2) : '';
  $('research-parameters').value = JSON.stringify(researchDraft.parameters || {}, null, 2);
  $('research-scheduling-mode').value = researchDraft.scheduling_mode || 'functional/1';
  $('research-joint').value = researchDraft.joint ? JSON.stringify(researchDraft.joint, null, 2) : '';
  hydrateResearchChoices(true);
  $('research-parent').value = researchDraft.parent_run_id || '';
  researchDirty = false;
  researchHydrated = true;
}

function readResearchConfig() {
  const algorithms = [...document.querySelectorAll('#research-form input[name="algorithm"]:checked')].map(input => input.value);
  if (!algorithms.length) throw new Error('至少选择一个学习器或固定参照');
  const seed = Number($('research-seed').value);
  const episodes = Number($('research-episodes').value);
  if (!Number.isInteger(seed) || seed < 0 || !Number.isInteger(episodes) || episodes < 1) throw new Error('种子需为非负整数，任务次数需为正整数');
  const config = structuredClone(researchDraft);
  Object.assign(config, {algorithm: algorithms[0], seed, episodes, mode: $('research-mode').value,
    parent_run_id: $('research-parent').value || null, split: $('research-split').value.trim(),
    tags: $('research-tags').value.split(/[,，]/).map(value => value.trim()).filter(Boolean),
    parameters: jsonField('research-parameters', '学习器参数'), scene: jsonField('research-scene', '场景配置'),
    criteria: readResearchCriteria()});
  if (algorithms.length > 1) config.algorithms = algorithms;
  else delete config.algorithms;
  const taskIds = $('research-task-ids').value.split(/[,，]/).map(value => value.trim()).filter(Boolean);
  if (taskIds.length) config.task_ids = taskIds;
  else delete config.task_ids;
  config.scene.unit_ids = [...document.querySelectorAll('#research-unit-options input:checked')].map(input => input.value);
  const independent = jsonField('research-independent', '独立评价', true);
  if (independent) config.evaluation_criteria = independent;
  else delete config.evaluation_criteria;
  const referenceFreeze = config.mode === 'freeze' && algorithms.every(algorithm =>
    ['required_only', 'all_applicable', 'simple'].includes(algorithm));
  if (config.mode !== 'train' && !config.parent_run_id && !referenceFreeze)
    throw new Error('学习器冻结评价、继续训练或条件重评需要选择来源运行');
  if ($('research-scheduling-mode').value === 'joint/1') {
    config.joint = jsonField('research-joint', '联合配置', true);
    if (!config.joint) throw new Error('joint/1 需要联合配置');
    config.scheduling_mode = 'joint/1';
  } else { delete config.scheduling_mode; delete config.joint; }
  return config;
}

function readResearchCriteria() {
  const criteria = jsonField('research-criteria', '评价资源');
  criteria.prompt = $('research-prompt').value;
  criteria.teacher = {...criteria.teacher, role: $('research-teacher-role').value};
  return criteria;
}

function hydrateResearchChoices(force = false, selectedOverride = null) {
  const parent = $('research-parent');
  const runs = researchState().runs || [];
  const parentValue = force ? researchDraft.parent_run_id || '' : parent.value;
  const signature = JSON.stringify(runs.map(run => [runIdentity(run), run.algorithm, run.status]));
  if (force || parent.dataset.options !== signature) {
    parent.replaceChildren(node('option', '无'));
    parent.firstChild.value = '';
    for (const run of runs) {
      const identity = runIdentity(run);
      const option = node('option', (learnerNames[run.algorithm] || run.algorithm || '运行') + ' · ' + identity + ' · ' + statusLabel(run.status));
      option.value = identity;
      parent.append(option);
    }
    if (parentValue && !runs.some(run => runIdentity(run) === parentValue)) {
      const option = node('option', parentValue + ' · 来源配置保留'); option.value = parentValue; parent.append(option);
    }
    parent.value = parentValue;
    parent.dataset.options = signature;
  }
  const root = $('research-unit-options');
  const units = (state?.units || []).filter(unit => !['SUPERSEDED', 'DETACHED'].includes(unit.status));
  const unitSignature = JSON.stringify(units.map(unit => [unit.id, unit.name, unit.status, unit.source_id]));
  if (force || selectedOverride || root.dataset.options !== unitSignature) {
    let selected;
    if (selectedOverride) selected = selectedOverride;
    else if (force) selected = new Set(researchDraft.scene?.unit_ids || []);
    else selected = new Set([...root.querySelectorAll('input:checked')].map(input => input.value));
    root.replaceChildren();
    for (const unit of units) {
      const source = (state.harnesses || []).find(item => item.id === unit.source_id);
      const label = node('label', undefined, 'research-unit-choice');
      const check = node('input'); check.type = 'checkbox'; check.value = unit.id; check.checked = selected.has(unit.id);
      const description = node('span');
      description.append(node('strong', unit.name || unit.id), node('small', (source?.name || unit.source_id || '') + ' · ' + statusLabel(unit.status)));
      check.disabled = unit.status !== 'READY' && !check.checked;
      check.onchange = () => {
        researchDirty = true;
        try {
          const scene = jsonField('research-scene', '场景配置');
          scene.unit_ids = [...root.querySelectorAll('input:checked')].map(input => input.value);
          $('research-scene').value = JSON.stringify(scene, null, 2);
        } catch (error) { $('research-form-status').textContent = error.message; }
      };
      label.append(check, description); root.append(label);
    }
    for (const identity of selected) {
      if (units.some(unit => unit.id === identity)) continue;
      const label = node('label', undefined, 'research-unit-choice');
      const check = node('input'); check.type = 'checkbox'; check.value = identity; check.checked = true;
      label.append(check, node('span', identity + ' · 当前能力池不可用')); root.append(label);
    }
    if (!units.length && !selected.size) empty(root, '当前没有可选 Unit。先分析 Harness 并补齐原资源。');
    root.dataset.options = unitSignature;
  }
  const role = $('research-teacher-role').value;
  const profile = state?.models?.[role];
  $('research-teacher-status').textContent = isReady(profile) ? profile.model + ' · ' + (role === 'analysis' ? '复用分析角色' : '独立教师') : '所选角色尚未配置';
}

function hydrateResearch() {
  if (!researchHydrated) loadResearchConfig(researchState().config?.config || researchState().config || blankResearchConfig());
  hydrateResearchChoices();
}

async function saveResearch(event) {
  event.preventDefault();
  const button = event.submitter;
  if (button) button.disabled = true;
  const status = $('research-form-status');
  try {
    const config = readResearchConfig();
    const result = await api('/api/scheduler/config', config);
    researchDraft = structuredClone(config); researchDirty = false;
    status.textContent = '已保存 · ' + (result.config_digest || result.id || '下次运行使用');
    await refresh();
  } catch (error) { status.textContent = error.message; }
  finally { if (button) button.disabled = false; }
}

async function startResearch() {
  const button = $('research-start');
  const status = $('research-form-status');
  button.disabled = true; status.textContent = '正在提交…';
  try {
    const config = readResearchConfig();
    const result = await api('/api/scheduler/runs', {config});
    const ids = result.run_ids || [result.run_id || result.id].filter(Boolean);
    for (const identity of ids) researchSelected.add(identity);
    status.textContent = '已提交 ' + ids.length + ' 次独立运行';
    showNotice('已提交运行：' + ids.join('、') + '。结果按时间与哈希保存。');
    await refresh();
  } catch (error) { status.textContent = error.message; }
  finally { button.disabled = false; }
}

function downloadJson(value, filename) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: 'application/json;charset=utf-8'}));
  const link = node('a'); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function exportResearchRun(identity, button) {
  button.disabled = true;
  try {
    const run = await api('/api/scheduler/runs/' + encodeURIComponent(identity));
    researchDetails.set(identity, run);
    const value = {schema: 'seampact-research-progress/1', exported_at: new Date().toISOString(),
      run_id: identity, status: run.status, partial: run.status !== 'COMPLETED',
      resumable_artifact: false, visibility: 'private',
      scope: 'saved task/window results, evidence, configuration and curves; may include local paths; excludes in-flight memory and policy weight files', run};
    downloadJson(value, identity + (value.partial ? '-progress' : '-results') + '.json');
    showNotice('已导出最新已保存结果，包含任务证据与本机路径。进行中的任务在收尾后进入结果；JSON 不包含策略权重文件。');
  } catch (error) { showNotice(error.message); }
  finally { button.disabled = false; }
}

const researchAutoShown = new Set();

function researchProgress(run) {
  const completed = run.episodes_completed ?? run.episodes;
  const budget = run.budget ?? run.total_episodes ?? run.manifest?.episodes;
  const root = node('div', undefined, 'run-progress');
  if (finiteNumber(completed) && finiteNumber(budget) && budget > 0) {
    const progress = node('progress'); progress.max = budget; progress.value = completed;
    progress.setAttribute('aria-label', '已保存任务或窗口 ' + completed + ' / ' + budget);
    root.append(progress, node('p', '已保存 ' + completed + ' / ' + budget + ' · 结果在任务／窗口边界更新', 'card-meta'));
  }
  return root;
}

function researchAction(label, path, body, completed) {
  const button = node('button', label, 'secondary-button'); button.type = 'button';
  button.onclick = async () => {
    button.disabled = true;
    try { await api(path, body); showNotice(completed); await refresh(); }
    catch (error) { showNotice(error.message); }
    finally { button.disabled = false; }
  };
  return button;
}

function renderResearch() {
  const research = researchState();
  for (const run of research.runs || []) {
    const identity = runIdentity(run);
    if (['RUNNING', 'QUEUED', 'STOPPING'].includes(run.status) && !researchAutoShown.has(identity)) {
      researchSelected.add(identity); researchAutoShown.add(identity);
    }
  }
  const readyRoot = $('research-readiness'); readyRoot.replaceChildren();
  if (!state?.scheduler?.research) empty(readyRoot, '当前进程尚未加载研究运行接口。');
  const readyUnits = (state.units || []).filter(unit => unit.status === 'READY').length;
  const summary = node('p', '能力池：' + readyUnits + ' 个源码已绑定入口 · 已保存运行：' + (research.runs || []).length, 'card-meta');
  readyRoot.append(summary);
  if (research.active_checkpoint) readyRoot.append(node('p', '当前冻结策略：' + textValue(research.active_checkpoint), 'card-note'));
  if (research.readiness) detailValue(readyRoot, '原环境、资源与入口条件', research.readiness);
  if (research.options) detailValue(readyRoot, '学习器可用配置', research.options);
  const filter = $('research-filter').value.trim().toLowerCase();
  const mode = $('research-mode-filter').value;
  const root = $('research-run-list'); root.replaceChildren();
  const runs = (research.runs || []).filter(run => {
    const sources = (run.source_ids || []).map(id => (state.harnesses || []).find(source => source.id === id)?.name || id);
    return (!mode || (run.mode || run.config?.mode) === mode) &&
      (!filter || JSON.stringify([run, sources]).toLowerCase().includes(filter));
  });
  if (!runs.length) empty(root, filter || mode ? '没有匹配的运行。可用完整哈希或时间查找。' : '尚无研究运行。配置真实场景及评价后开始。');
  for (const run of runs) {
    const identity = runIdentity(run);
    const card = node('article', undefined, 'status-card research-run'); card.dataset.itemId = identity;
    const head = node('div', undefined, 'card-heading');
    const select = node('label', undefined, 'research-run-select');
    const check = node('input'); check.type = 'checkbox'; check.checked = researchSelected.has(identity); check.setAttribute('aria-label', '加入对比 ' + identity);
    check.onchange = () => {
      if (check.checked) researchSelected.add(identity); else researchSelected.delete(identity);
      renderResearchComparison(); refreshResearchDetails();
    };
    select.append(check, node('strong', learnerNames[run.algorithm] || run.algorithm || '运行')); head.append(select, pill(run.status)); card.append(head);
    card.append(node('p', identity, 'research-identity'));
    card.append(node('p', [researchModeNames[run.mode] || run.mode, run.split, run.started_at || run.created_at,
      run.episodes !== undefined ? '已完成 ' + (run.episodes_completed ?? run.episodes) + ' / ' + (run.budget ?? run.total_episodes ?? '—') : null].filter(Boolean).join(' · '), 'card-meta'));
    const checkpointId = run.checkpoint_id || run.checkpoint?.checkpoint_id;
    card.append(node('p', '配置 ' + shortHash(run.config_digest) + (checkpointId ? ' · 策略 ' + shortHash(checkpointId) : ''), 'card-meta'));
    if (run.eligible_episodes !== undefined) card.append(node('p', '可学习任务 ' + run.eligible_episodes + ' · 缺失而排除 ' + (run.excluded_episodes ?? 0) +
      ' · 有取舍交互 ' + (run.choice_interactions ?? 0) + ' · 更新 ' + (run.updates ?? 0), 'card-meta'));
    if (run.tags?.length) card.append(node('p', run.tags.join(' · '), 'card-note'));
    if (run.error) card.append(node('p', textValue(run.error), 'message-error card-note'));
    card.append(researchProgress(run));
    const actions = node('div', undefined, 'harness-actions');
    const view = node('button', '查看与对比', 'secondary-button'); view.type = 'button';
    view.onclick = () => { researchSelected.add(identity); check.checked = true; renderResearchComparison(); refreshResearchDetails(); };
    actions.append(view);
    const exportButton = node('button', '导出当前结果', 'secondary-button'); exportButton.type = 'button';
    exportButton.onclick = () => exportResearchRun(identity, exportButton); actions.append(exportButton);
    if (['QUEUED', 'RUNNING', 'STOPPING'].includes(run.status)) actions.append(researchAction('在任务边界停止', '/api/scheduler/runs/stop', {run_id: identity}, '已请求在当前任务收尾后停止。'));
    if (checkpointId || run.artifacts?.checkpoint) actions.append(researchAction('启用冻结策略', '/api/scheduler/activate', {run_id: identity}, '已请求启用该运行的兼容冻结策略。'));
    card.append(actions); root.append(card);
  }
  renderResearchComparison(); refreshResearchDetails();
}

async function refreshResearchDetails() {
  const runs = researchState().runs || [];
  await Promise.all([...researchSelected].map(async identity => {
    const summary = runs.find(run => runIdentity(run) === identity);
    const version = JSON.stringify(summary || identity);
    if (researchLoading.has(identity) || researchDetailVersions.get(identity) === version) return;
    researchLoading.add(identity);
    try {
      researchDetails.set(identity, await api('/api/scheduler/runs/' + encodeURIComponent(identity)));
      researchDetailVersions.set(identity, version);
    } catch (error) { researchDetails.set(identity, {id: identity, error: error.message}); }
    finally { researchLoading.delete(identity); }
  }));
  renderResearchComparison();
}

function runMetrics(run) {
  const provided = run.metrics || run.summary?.metrics;
  if (Array.isArray(provided)) return provided;
  if (provided && typeof provided === 'object') return Object.entries(provided).map(([id, value]) =>
    value && typeof value === 'object' ? {id, ...value} : {id, value});
  const groups = new Map();
  for (const item of run.history || []) {
    const list = item.feedback?.metrics || item.evaluation?.metrics || [];
    for (const metric of list) {
    const route = metric.route || item.route;
    const key = JSON.stringify([route, metric.scope, metric.id, metric.definition, metric.version, metric.unit, metric.direction]);
      if (!groups.has(key)) groups.set(key, {...metric, route, values: [], known: 0, unknown: 0, not_applicable: 0, denominator: 0});
      const group = groups.get(key);
      if (metric.status === 'KNOWN' && metric.value !== null && finiteNumber(Number(metric.value))) { group.values.push(Number(metric.value)); group.known++; }
      else if (metric.status === 'NOT_APPLICABLE') group.not_applicable++;
      else group.unknown++;
      group.denominator += metric.denominator ?? (metric.status === 'NOT_APPLICABLE' ? 0 : 1);
    }
  }
  return [...groups.values()].map(group => ({...group, status: group.known ? 'KNOWN' : group.not_applicable && !group.unknown ? 'NOT_APPLICABLE' : 'UNKNOWN',
    value: group.values.length ? group.values.reduce((sum, value) => sum + value, 0) / group.values.length : null}));
}

function curvePoints(run) {
  const curves = run.curves;
  if (Array.isArray(curves)) return curves.map((item, index) => typeof item === 'number' ? {x: index + 1, y: item} :
    {x: item.episode ?? item.step ?? index + 1, y: item.reward ?? item.return ?? null});
  const rewards = curves?.reward || curves?.rewards;
  if (Array.isArray(rewards)) return rewards.map((value, index) => ({x: index + 1, y: typeof value === 'object' ? value.value : value}));
  return (run.history || []).map((item, index) => ({x: item.episode ?? index + 1, y: item.feedback?.reward ?? item.reward ?? null}));
}

function curveFigure(run) {
  const points = curvePoints(run);
  const known = points.filter(point => finiteNumber(point.x) && finiteNumber(point.y));
  if (!known.length) return node('p', '尚无可用的任务回报曲线；UNKNOWN 不按零绘制。', 'empty-note');
  const figure = node('figure', undefined, 'research-curve');
  const ns = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(ns, 'svg'); svg.setAttribute('viewBox', '0 0 600 180'); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', '实际任务回报曲线');
  const lowX = Math.min(...known.map(point => point.x)), highX = Math.max(...known.map(point => point.x));
  const lowY = Math.min(...known.map(point => point.y)), highY = Math.max(...known.map(point => point.y));
  const px = point => 45 + (point.x - lowX) / (highX - lowX || 1) * 540;
  const py = point => 145 - (point.y - lowY) / (highY - lowY || 1) * 125;
  const axis = document.createElementNS(ns, 'path'); axis.setAttribute('d', 'M45 15 V145 H585'); axis.setAttribute('class', 'curve-axis'); svg.append(axis);
  let segment = [];
  const appendSegment = () => {
    if (!segment.length) return;
    const path = document.createElementNS(ns, 'polyline'); path.setAttribute('points', segment.join(' ')); path.setAttribute('class', 'curve-line'); svg.append(path); segment = [];
  };
  for (const point of points) {
    if (!finiteNumber(point.x) || !finiteNumber(point.y)) { appendSegment(); continue; }
    segment.push(px(point) + ',' + py(point));
    const dot = document.createElementNS(ns, 'circle'); dot.setAttribute('cx', px(point)); dot.setAttribute('cy', py(point)); dot.setAttribute('r', '2.5'); dot.setAttribute('class', 'curve-point');
    const title = document.createElementNS(ns, 'title'); title.textContent = '任务 ' + point.x + '：' + point.y; dot.append(title); svg.append(dot);
  }
  appendSegment();
  for (const [x, y, text] of [[45, 167, lowX], [555, 167, highX], [2, 24, displayNumber(highY)], [2, 146, displayNumber(lowY)]]) {
    const label = document.createElementNS(ns, 'text'); label.setAttribute('x', x); label.setAttribute('y', y); label.textContent = text; svg.append(label);
  }
  figure.append(svg, node('figcaption', '横轴为任务序号，纵轴为该运行的实际回报。缺失值断开，目标配置不同的回报不直接排名。'));
  return figure;
}

function configDifference(left, right, path = '', result = []) {
  if (JSON.stringify(left) === JSON.stringify(right)) return result;
  if (left && right && typeof left === 'object' && typeof right === 'object' && !Array.isArray(left) && !Array.isArray(right)) {
    for (const key of new Set([...Object.keys(left), ...Object.keys(right)])) configDifference(left[key], right[key], path ? path + '.' + key : key, result);
  } else result.push({field: path, first: left === undefined ? null : left, other: right === undefined ? null : right});
  return result;
}

function jointRunSummary(card, run) {
  const manifest = run.manifest || {};
  if (manifest.scheduling_mode !== 'joint/1') return;
  const joint = manifest.joint || {};
  card.append(node('p', '功能 ' + (joint.functional_policy ?? '—') + ' · 派发 ' + (joint.dispatch_policy ?? '—') +
    ' · 资源 ' + (joint.resource_policy ?? '—') + ' · 根调用槽 ' + (joint.capacity?.call_slots ?? '—') +
    ' · 副本上限 ' + (joint.capacity?.replica_cap ?? '—'), 'card-meta'));
  card.append(node('p', '调度范围：' + (joint.dispatch_scope === 'roots_and_boundaries' ? '根请求及已核验内部边界' : '根请求') +
    ' · 工作集：' + (joint.working_sets ? '按任务准入，允许能力池大于驻留容量' : '历史配置') +
    ' · 资源采样：' + (joint.observation ? '随窗口记录' : '未配置'), 'card-meta'));
  const summary = run.joint;
  if (summary) {
    const means = (summary.mean_active_roots || []).filter(value => finiteNumber(value));
    card.append(node('p', '窗口 ' + (summary.windows ?? '—') + ' · 任务 ' + (summary.tasks ?? '—') +
      ' · 未完成 ' + (summary.unfinished_tasks ?? '—') + ' · 失败 ' + (summary.failed_tasks ?? '—') +
      ' · 最大并行根调用 ' + (summary.max_active_roots ?? '—') + ' · 平均并行度 ' +
      displayNumber(means.length ? means.reduce((sum, value) => sum + value, 0) / means.length : null), 'card-meta'));
  }
  const counters = summary?.counters || {};
  const actions = node('table', undefined, 'research-metrics');
  const actionHead = node('thead'), actionHeader = node('tr');
  for (const label of ['动作', '次数']) actionHeader.append(node('th', label));
  actionHead.append(actionHeader); actions.append(actionHead);
  const actionBody = node('tbody');
  for (const [label, name] of [['invoke', '功能调用'], ['continue', '推进'], ['finish', '结束'], ['dispatch', '派发'],
    ['prepare', '提前准备'], ['scale_out', '水平扩容'], ['scale_in', '水平缩容'], ['yield_until_next_event', '保留等待']]) {
    const row = node('tr'); row.append(node('td', name), node('td', counters['action/' + label] || 0)); actionBody.append(row);
  }
  for (const key of Object.keys(counters)) {
    if (!(key.startsWith('business/') || key.startsWith('dispatch/') || key.startsWith('boundary/') || key.startsWith('resource/') || key.startsWith('on_demand/'))) continue;
    const row = node('tr'); row.append(node('td', key), node('td', counters[key] || 0)); actionBody.append(row);
  }
  actions.append(actionBody); card.append(actions);
  const table = node('table', undefined, 'research-metrics');
  const thead = node('thead'), header = node('tr');
  for (const label of ['窗口', '回报', '并行度(平均/最大)', '并行时间占比', '任务秒', '实例秒', '资源操作', '状态']) header.append(node('th', label));
  thead.append(header); table.append(thead);
  const tbody = node('tbody');
  for (const item of run.history || []) {
    if (item.kind !== 'joint_window') continue;
    const row = node('tr');
    row.append(node('td', item.episode ?? '—'),
      node('td', item.reward == null ? (item.eligible ? '—' : 'UNKNOWN') : displayNumber(item.reward)),
      node('td', displayNumber(item.parallelism?.mean_active_roots) + ' / ' + (item.parallelism?.max_active_roots ?? '—')),
      node('td', displayNumber(item.parallelism?.parallel_fraction)),
      node('td', displayNumber(item.parallelism?.task_seconds)),
      node('td', displayNumber(item.parallelism?.instance_seconds)),
      node('td', (item.resource_ops || []).length),
      node('td', item.eligible ? '有效' : (item.exclusion_reason || '排除')));
    tbody.append(row);
  }
  table.append(tbody);
  if (tbody.childNodes.length) card.append(table);
  for (const item of run.history || []) {
    if (item.kind !== 'joint_window') continue;
    const details = node('details'), title = node('summary', '窗口 ' + item.episode + ' · 实测资源与任务覆盖');
    details.append(title);
    const service = item.service || {};
    details.append(node('p', '到达 ' + (service.arrived ?? '—') + ' · 按时完成 ' + (service.completed ?? '—') +
      ' · 完成子集 p95 ' + displayNumber(service.response_p95_ms) + ' ms · 延迟分母 ' + (service.response_denominator ?? '—')));
    const observed = item.resource_observation;
    if (!observed || observed.status === 'NOT_CONFIGURED') details.append(node('p', '本窗口未配置物理资源采样。'));
    else {
      for (const device of observed.devices || []) details.append(node('p', 'GPU 显存积分 ' + displayNumber(device.memory_mib_s) +
        ' MiB·s · 采样峰值 ' + displayNumber(device.peak_sampled_memory_mib) + ' MiB · 设备能耗估计 ' +
        displayNumber(device.device_energy_j_estimate) + ' J · 显存观测覆盖 ' + displayNumber(device.metrics?.memory_used_mib?.valid_time_coverage)));
      for (const process of observed.processes || []) details.append(node('p', '所选进程 CPU ' + displayNumber(process.cpu_seconds_delta) +
        ' s · RSS 采样峰值 ' + displayNumber(process.peak_sampled_rss_bytes) + ' bytes · CPU 观测覆盖 ' + displayNumber(process.cpu_time_coverage)));
      details.append(node('p', '设备、进程与容器范围分别报告；设备功率可能包含共享使用。采样空缺不计为零，实例秒是逻辑代理。', 'card-note'));
    }
    const evidence = node('pre'); evidence.textContent = JSON.stringify({routes:item.route_metrics, service:item.service,
      observation:item.resource_observation}, null, 2); details.append(evidence); card.append(details);
  }
  card.append(node('p', '并行度按同时执行的根调用计算，不以实例数量代替；窗口回报＝已归属到学习转移的部分＋未归属部分。', 'card-note'));
}

function renderResearchComparison() {
  const root = $('research-comparison'); root.replaceChildren();
  if (!researchSelected.size) return;
  root.append(node('h3', '所选运行 · 分项对照'));
  root.append(node('p', '按原始运行身份并列。指标定义、版本、单位或判据不同的结果需结合配置差异解释；UNKNOWN 与 N/A 保留。', 'card-note'));
  const grid = node('div', undefined, 'research-comparison-grid');
  const selected = [...researchSelected];
  for (const identity of selected) {
    const run = researchDetails.get(identity);
    const card = node('article', undefined, 'status-card'); card.dataset.itemId = identity;
    const head = node('div', undefined, 'card-heading'); head.append(node('strong', String(identity).slice(-14)));
    const close = node('button', '移出对照', 'icon-button'); close.type = 'button';
    close.onclick = () => { researchSelected.delete(identity); renderResearch(); };
    head.append(close); card.append(head, node('p', identity, 'research-identity'));
    if (!run) { empty(card, '正在读取结果…'); grid.append(card); continue; }
    if (run.error) card.append(node('p', textValue(run.error), 'message-error card-note'));
    const manifest = run.manifest || {};
    card.append(node('p', (learnerNames[run.algorithm || manifest.algorithm] || run.algorithm || manifest.algorithm || '') + ' · ' + statusLabel(run.status), 'card-meta'));
    card.append(researchProgress(run));
    const table = node('table', undefined, 'research-metrics');
    const thead = node('thead'), header = node('tr'); for (const label of ['指标', '原值 / 均值', '覆盖']) header.append(node('th', label)); thead.append(header); table.append(thead);
    const tbody = node('tbody');
    for (const metric of runMetrics(run)) {
      const row = node('tr'), name = node('td', metric.name || metric.id || '指标');
      name.append(node('small', [metric.scope === 'independent' ? '独立评价' : metric.scope === 'training' ? '训练反馈' : metric.scope,
        metric.route, metric.version, metric.unit, metric.direction === 'minimize' ? '越低越好' : metric.direction === 'maximize' ? '越高越好' : ''].filter(Boolean).join(' · ')));
      name.title = metric.definition || '';
      const value = metric.status === 'NOT_APPLICABLE' ? 'N/A' : metric.status === 'UNKNOWN' ? 'UNKNOWN' : displayNumber(metric.value ?? metric.mean);
      const counts = metric.status_counts;
      const coverage = counts ? (counts.KNOWN || 0) + ' / ' + metric.population + ' · 未知 ' + (counts.UNKNOWN || 0) + ' · N/A ' + (counts.NOT_APPLICABLE || 0) :
        metric.known !== undefined ? metric.known + ' / ' + metric.denominator + (metric.unknown ? ' · 未知 ' + metric.unknown : '') : metric.status || '—';
      row.append(name, node('td', value), node('td', coverage)); tbody.append(row);
    }
    table.append(tbody);
    if (tbody.childNodes.length) card.append(table); else empty(card, '暂无分项结果，未将缺失记为零。');
    card.append(curveFigure(run));
    jointRunSummary(card, run);
    detailValue(card, '不可变运行清单与配置', manifest);
    if (run.artifacts) detailValue(card, '结果与策略工件', run.artifacts);
    if (run.checkpoint) detailValue(card, '策略工件及内容摘要', run.checkpoint);
    if (run.learning) detailValue(card, '学习更新状态', run.learning);
    if (run.costs) detailValue(card, '实测成本与采集覆盖', run.costs);
    if (run.summary) detailValue(card, '完整结果摘要', run.summary);
    const exportButton = node('button', '导出当前结果', 'secondary-button'); exportButton.type = 'button';
    exportButton.onclick = () => exportResearchRun(identity, exportButton); card.append(exportButton);
    grid.append(card);
  }
  root.append(grid);
  if (selected.length > 1) {
    const first = researchDetails.get(selected[0]);
    if (first) for (const identity of selected.slice(1)) {
      const other = researchDetails.get(identity); if (!other) continue;
      const differences = configDifference(first.manifest?.config || first.manifest || first.config,
        other.manifest?.config || other.manifest || other.config);
      detailValue(root, '配置差异 · ' + shortHash(selected[0]) + ' / ' + shortHash(identity), differences.length ? differences : '保存的配置内容相同；运行身份与真实结果仍各自独立。');
    }
  }
}

function resourceState() { return state?.resources || {}; }
function resourceId(record) { return record?.id || record?.connection_id || record?.resource_id; }
function connectionOptions(select, value) {
  const connections = entries(resourceState().connections);
  const signature = JSON.stringify(connections.map(item => [resourceId(item), item.name, item.model]));
  if (select.dataset.options !== signature) {
    const previous = select.value;
    const option = node('option', '选择命名连接'); option.value = '';
    select.replaceChildren(option);
    for (const connection of connections) {
      const item = node('option', connection.name + (connection.model ? ' · ' + connection.model : ''));
      item.value = resourceId(connection); select.append(item);
    }
    select.value = previous;
    select.dataset.options = signature;
  }
  if (value !== undefined) select.value = value || '';
}

function resourceStatus(root, label, value, note) {
  const row = node('div', undefined, 'resource-state');
  row.append(node('span', label), node('strong', value || '未检查'));
  if (note) row.append(node('small', note));
  root.append(row);
}

function resourceExecution(record) {
  return typeof record.execution === 'string' ? record.execution : record.execution?.status ||
    record.target_execution || record.check?.target_execution || 'NOT_RUN';
}

function declaredCapabilities(connection) {
  return connection.service_capabilities || (Array.isArray(connection.capabilities) ? connection.capabilities : []);
}

function selectCredentialConnection(id) {
  const form = $('connection-credential-form');
  if (form.elements.connection_id.value !== id) form.elements.credential.value = '';
  connectionOptions(form.elements.connection_id, id);
}

function readableResourceStatus(status, kind) {
  const labels = {NOT_RUN: 'NOT_RUN · 未执行', NOT_CHECKED: '未检查', UNCHECKED: '未检查',
    MISSING: '缺失', UNREADABLE: '不可读', FAILED: '失败', SUCCEEDED: '实际调用成功', UNKNOWN: '无法确定',
    MATCHED: '匹配', MISMATCH: '不匹配', MISMATCHED: '不匹配', AVAILABLE: '可访问',
    ACCESSIBLE: '可访问', CONFIGURED: '已配置', SAVED: '选择已保存', SELECTED: '选择已保存',
    MAPPING_CHECKED: '原字段映射已核对', BOUND: '映射已核对',
    VERIFIED: '已核对', FILES_PRESENT: '必要文件存在', PRESENT: '必要文件存在', WRONG_TYPE: '路径类型不符',
    INVALID_MANIFEST: '文件清单无效', INVALID_ARTIFACT: '文件仍为下载指针，实际内容未取得',
    CHECKSUM_OR_SIZE_MISMATCH: '摘要或文件大小不符',
    UNSUPPORTED_PATH: '执行环境不支持此路径', MODELS_LIST_ACCESSIBLE: '模型列表可访问',
    AUTHENTICATION_FAILED: '认证失败', ACCESSIBLE_HTTP_ERROR: '地址可达，服务返回错误',
    CONNECTION_FAILED: '连接失败', INVALID_RESPONSE: '响应格式不匹配',
    CREDENTIAL_MISSING: '缺少凭据', CREDENTIAL_UNREADABLE: '凭据读取失败',
    RESOLVABLE: '可解析', NOT_REQUIRED: '无需认证',
    READY: kind === 'weight' ? '必要文件存在' : '可访问'};
  return labels[String(status || '').toUpperCase()] || status || '未检查';
}

function credentialSummary(connection) {
  const auth = connection.auth || {};
  const mode = auth.mode || connection.auth_mode;
  if (mode === 'none') return '明确无需认证';
  const error = connection.credential_error || auth.error;
  if (error) return '读取失败 · ' + error;
  const status = connection.credential_status || auth.status || connection.credential?.status;
  if (status) return readableResourceStatus(status);
  if (mode === 'environment') return '环境变量 ' + (auth.env || connection.env || '未指定') + ' · ' +
    (connection.key_present || auth.available ? '可解析' : '尚未确认可取得');
  return connection.key_present || auth.available || connection.credential?.available ? '已配置，可解析' : '未配置或尚未确认可解析';
}

async function resourceAction(button, endpoint, body, message) {
  button.disabled = true;
  try { await api(endpoint, body); showNotice(message); await refresh(); }
  catch (error) { showNotice(error.message); }
  finally { button.disabled = false; }
}

function editConnection(connection) {
  const form = $('connection-form');
  form.elements.id.value = resourceId(connection) || '';
  for (const field of ['name', 'base_url', 'model', 'service_version']) form.elements[field].value = connection[field] || '';
  form.elements.protocol.value = connection.protocol || 'openai-compatible';
  form.elements.parameters.value = textValue(connection.parameters || {});
  form.elements.auth_mode.value = connection.auth?.mode || 'none';
  form.elements.auth_env.value = connection.auth?.env || '';
  for (const input of form.querySelectorAll('[name="capability"]')) input.checked = declaredCapabilities(connection).includes(input.value);
  form.dataset.savedUrl = connection.base_url || '';
  $('connection-form-title').textContent = '编辑连接 · ' + (connection.name || resourceId(connection));
  $('connection-form-status').textContent = '';
  connectionAuthChanged();
  selectCredentialConnection(resourceId(connection));
  form.scrollIntoView({block: 'nearest'});
}

function connectionAuthChanged() {
  const form = $('connection-form');
  form.elements.auth_env.disabled = form.elements.auth_mode.value !== 'environment';
  form.elements.auth_env.required = form.elements.auth_mode.value === 'environment';
}

function connectionOperationChecks(connection) {
  const checks = connection.operation_checks || {};
  return Array.isArray(checks) ? checks : Object.entries(checks).map(([operation, result]) => ({operation, ...result}));
}

function renderConnectionOperation(root, result) {
  if (!result) return;
  const heading = [result.operation, readableResourceStatus(result.status), result.checked_at].filter(Boolean).join(' · ');
  root.append(node('p', heading, 'card-meta'));
  if (result.latency_ms != null) root.append(node('p', '调用耗时：' + result.latency_ms + ' ms', 'card-meta'));
  if (result.usage) root.append(node('p', '服务返回用量：' + textValue(result.usage), 'card-meta'));
  if (result.request_parameters) detailValue(root, '本次请求参数', result.request_parameters);
  if (result.response !== undefined && result.response !== null) root.append(node('pre', textValue(result.response), 'operation-response'));
  if (result.reason) root.append(node('p', result.reason, 'card-note'));
  root.append(node('p', '此连接操作不执行 Harness 原目标；真实目标执行见 Harness 依赖记录。', 'card-meta'));
}

function updateConnectionInvocation() {
  const form = $('connection-invoke-form');
  connectionOptions(form.elements.connection_id);
  const connection = entries(resourceState().connections).find(item => resourceId(item) === form.elements.connection_id.value);
  const operations = connection?.protocol === 'openai-compatible' ?
    ['text', 'image', 'embedding'].filter(operation => declaredCapabilities(connection).includes(operation)) : [];
  const selector = form.elements.operation;
  const signature = JSON.stringify([resourceId(connection), operations]);
  if (selector.dataset.options !== signature) {
    const previous = selector.value;
    const prompt = node('option', operations.length ? '选择实际调用操作' : '没有可调用的兼容操作'); prompt.value = '';
    selector.replaceChildren(prompt);
    for (const operation of operations) {
      const option = node('option', {text: '文本生成', image: '图像理解', embedding: 'Embedding'}[operation]);
      option.value = operation; selector.append(option);
    }
    selector.value = operations.includes(previous) ? previous : '';
    selector.dataset.options = signature;
  }
  const image = selector.value === 'image';
  $('connection-invoke-image-label').hidden = !image;
  form.elements.image_path.disabled = !image;
  form.elements.image_path.required = image;
  form.querySelector('[type="submit"]').disabled = !selector.value || form.dataset.submitting === 'true';
}

async function invokeConnection(event) {
  event.preventDefault();
  const form = event.currentTarget, status = $('connection-invoke-status'), root = $('connection-invoke-result');
  if (!form.elements.input.value.trim()) { status.textContent = '请填写本次实际发送的明确输入'; return; }
  const connection = entries(resourceState().connections).find(item => resourceId(item) === form.elements.connection_id.value);
  const operation = form.elements.operation.value;
  if (connection?.protocol !== 'openai-compatible' || !['text', 'image', 'embedding'].includes(operation) || !declaredCapabilities(connection).includes(operation)) {
    status.textContent = '请先选择已声明对应能力的 OpenAI 兼容连接'; return;
  }
  const body = {connection_id: resourceId(connection), operation, input: form.elements.input.value};
  if (operation === 'image') {
    if (!form.elements.image_path.value.trim()) { status.textContent = '请填写平台实际执行环境可见的图像路径'; return; }
    body.image_path = form.elements.image_path.value.trim();
  }
  form.dataset.submitting = 'true'; updateConnectionInvocation();
  status.textContent = '正在使用本次输入调用连接能力'; root.replaceChildren();
  try {
    const result = await api('/api/connections/invoke', body);
    renderConnectionOperation(root, result);
    status.textContent = '连接能力调用已记录；Harness 原目标执行见依赖记录';
    await refresh();
  } catch (error) { status.textContent = error.message; }
  finally { delete form.dataset.submitting; updateConnectionInvocation(); }
}

function renderConnections() {
  const root = $('connection-list'); root.replaceChildren();
  const connections = entries(resourceState().connections);
  if (!connections.length) empty(root, '尚未登记命名连接。可先保存地址，再单独保存私有凭据。');
  for (const connection of connections) {
    const card = node('article', undefined, 'resource-card'); card.dataset.itemId = resourceId(connection);
    card.append(node('h4', connection.name || resourceId(connection)), node('p', connection.base_url, 'card-meta'));
    card.append(node('p', [connection.protocol, connection.model || '无固定模型 ID',
      '版本 ' + (connection.service_version || 'UNKNOWN')].filter(Boolean).join(' · '), 'card-meta'));
    const checks = connection.check || connection.checks || {};
    const operations = connectionOperationChecks(connection);
    const statuses = node('div', undefined, 'resource-states');
    resourceStatus(statuses, '凭据', credentialSummary(connection));
    resourceStatus(statuses, '服务连接', readableResourceStatus(checks.status || connection.connection_status), checks.reason || checks.error);
    resourceStatus(statuses, '连接操作调用', operations.length ?
      operations.map(result => result.operation + '：' + readableResourceStatus(result.status)).join('；') :
      readableResourceStatus(checks.capability_status || connection.capability_status || 'NOT_RUN'),
      '声明：' + (declaredCapabilities(connection).join('、') || '尚未声明'));
    resourceStatus(statuses, '操作范围', '连接检查与明确能力调用',
      '这些操作不执行 Harness 原目标；真实目标执行见 Harness 依赖记录。');
    card.append(statuses);
    if (operations.length) {
      const detail = node('details', undefined, 'connection-operation-checks');
      detail.append(node('summary', '最近明确触发的连接能力调用'));
      for (const result of operations) renderConnectionOperation(detail, result);
      card.append(detail);
    }
    if (checks.checked_at) card.append(node('p', '连接检查时间：' + checks.checked_at, 'card-meta'));
    if (connection.revision) card.append(node('p', '配置版本：' + connection.revision, 'card-meta'));
    else if (connection.digest) card.append(node('p', '配置摘要：' + connection.digest, 'card-meta'));
    const actions = node('div', undefined, 'harness-actions');
    const edit = node('button', '编辑配置', 'secondary-button'); edit.type = 'button'; edit.onclick = () => editConnection(connection);
    const check = node('button', '检查连接', 'secondary-button'); check.type = 'button';
    check.onclick = () => resourceAction(check, '/api/connections/check', {connection_id: resourceId(connection)},
      '连接检查已记录。模型列表或协议入口可访问不代表图像、工具、embedding 或原目标已运行。');
    const credential = node('button', '设置凭据', 'secondary-button'); credential.type = 'button';
    credential.onclick = () => {
      selectCredentialConnection(resourceId(connection));
      $('connection-credential-form').scrollIntoView({block: 'nearest'});
    };
    const invoke = node('button', '输入并调用能力', 'secondary-button'); invoke.type = 'button';
    invoke.onclick = () => {
      connectionOptions($('connection-invoke-form').elements.connection_id, resourceId(connection));
      updateConnectionInvocation(); $('connection-invoke-form').scrollIntoView({block: 'nearest'});
    };
    actions.append(edit, check, credential, invoke); card.append(actions); root.append(card);
  }
  connectionOptions($('connection-credential-form').elements.connection_id);
  updateConnectionInvocation();
}

function editWeight(weight) {
  const form = $('weight-form');
  form.elements.id.value = resourceId(weight) || '';
  for (const field of ['name', 'path', 'model_revision']) form.elements[field].value = weight[field] || '';
  form.elements.kind.value = weight.kind || 'directory';
  const required = weight.required_files || [];
  form.elements.required_files.value = required.some(item => typeof item === 'object' && Object.keys(item).some(key => key !== 'path')) ?
    textValue(required) : required.map(item => typeof item === 'string' ? item : item.path).join('\n');
  $('weight-form-title').textContent = '编辑权重 · ' + (weight.name || resourceId(weight));
  $('weight-form-status').textContent = '';
  form.scrollIntoView({block: 'nearest'});
}

function renderWeights() {
  const root = $('weight-list'); root.replaceChildren();
  const weights = entries(resourceState().weights);
  if (!weights.length) empty(root, '尚未登记权重路径。路径可以暂缺；检查会明确显示实际缺失项。');
  for (const weight of weights) {
    const card = node('article', undefined, 'resource-card'); card.dataset.itemId = resourceId(weight);
    card.append(node('h4', weight.name || resourceId(weight)), node('p', weight.path, 'resource-path'));
    card.append(node('p', '执行环境：' + (weight.execution_environment || 'local') + ' · ' +
      (weight.kind === 'file' ? '单个文件' : '目录') + ' · 模型版本：' + (weight.model_revision || 'UNKNOWN'), 'card-meta'));
    const check = weight.check || weight.checks || {};
    const statuses = node('div', undefined, 'resource-states');
    const filesStatus = String(check.status).toUpperCase() === 'PRESENT' && check.necessary_files === 'not_declared' ?
      '路径存在 · 必要文件未声明' : readableResourceStatus(check.status || weight.status, 'weight');
    resourceStatus(statuses, '文件检查', filesStatus, check.reason || check.error);
    resourceStatus(statuses, '检查范围', '路径与必要文件',
      '文件检查不加载模型或执行 Harness 原目标；真实目标执行见 Harness 依赖记录。');
    card.append(statuses);
    const missing = check.missing || check.missing_files || weight.missing_files;
    if (missing?.length) card.append(node('p', '缺失：' + missing.join('、'), 'card-note message-error'));
    if (check.invalid_files?.length) card.append(node('p', '需取得实际文件：' +
      check.invalid_files.map(item => item.path).join('、'), 'card-note message-error'));
    if (check.path || check.resolved_path) card.append(node('p', '实际检查位置：' + (check.resolved_path || check.path), 'resource-path'));
    if (weight.required_files?.length) detailValue(card, '登记的必要文件', weight.required_files);
    if (check.checked_at) card.append(node('p', '检查时间：' + check.checked_at, 'card-meta'));
    detailValue(card, '资源身份及只读检查', check);
    const actions = node('div', undefined, 'harness-actions');
    const edit = node('button', '编辑路径', 'secondary-button'); edit.type = 'button'; edit.onclick = () => editWeight(weight);
    const action = node('button', '检查资源', 'secondary-button'); action.type = 'button';
    action.onclick = () => resourceAction(action, '/api/resources/check', {resource_id: resourceId(weight)},
      '已核对路径及必要文件。本次检查未导入原库、加载模型或发起推理。');
    actions.append(edit, action); card.append(actions); root.append(card);
  }
}

function dependencyPresentation(dependency, binding, harness) {
  const mappingReady = dependency.configurable === false ? dependency.status === 'MAPPING_CHECKED' :
    (dependency.selection_allowed ?? dependency.status === 'MAPPING_CHECKED');
  const bindingAllowed = mappingReady && !['DETACHED', 'ANALYZING'].includes(harness.status);
  const partial = mappingReady && dependency.status !== 'MAPPING_CHECKED';
  const noun = dependency.kind === 'service' ? '服务连接' : '模型文件';
  let title, action;
  if (harness.status === 'DETACHED') {
    title = '来源已停用'; action = '重新接入后，再配置所需资源。';
  } else if (harness.status === 'ANALYZING') {
    title = '正在分析'; action = '请等分析结束后再配置资源。';
  } else if (!mappingReady) {
    title = '待平台处理'; action = '平台还没确认原代码如何使用这项资源。需要继续修正接线；现在不用重复添加服务或模型文件。';
  } else if (dependency.configurable === false) {
    title = '需要检查文件'; action = '请把文件放到原代码要求的位置，再检查文件是否齐全。';
  } else if (!binding) {
    title = '待选择' + noun; action = '接线检查已通过，请在下方选择对应的' + noun + '。';
  } else if (binding.reason || !['BOUND', 'MAPPING_CHECKED', 'READY', 'CONFIGURED', 'SAVED', 'SELECTED', 'VERIFIED'].includes(String(binding.status).toUpperCase())) {
    title = '需要检查所选资源'; action = '资源已选过，但还没有确认可用。请检查配置或文件，再保存并重新检查。';
  } else {
    title = '资源已选择'; action = '接下来实际运行一次，确认这项功能能正常工作。';
  }
  return {mappingReady, bindingAllowed, partial, noun, title, action};
}

function harnessDependencies(card, harness) {
  const dependencies = entries(resourceState().dependencies).filter(item => (item.source_id || item.harness_id) === harness.id);
  const detail = node('details', undefined, 'dependency-details');
  detail.open = dependencies.some(dependency => dependency.status !== 'MAPPING_CHECKED');
  detail.append(node('summary', '所需资源 · ' + dependencies.length + ' 项'));
  if (!dependencies.length) {
    detail.append(node('p', '分析器还没有列出所需资源，请先完成来源分析。', 'card-note'));
    card.append(detail); return;
  }
  const bindings = entries(resourceState().bindings);
  for (const dependency of dependencies) {
    const id = dependency.id || dependency.dependency_id;
    const binding = bindings.find(item => item.dependency_id === id);
    const row = node('article', undefined, 'dependency-card'); row.dataset.itemId = id;
    const heading = String(dependency.name || dependency.description || (dependency.kind === 'service' ? '服务连接' : '模型文件')).split(/[。\n]/)[0];
    row.append(node('h4', heading.length > 90 ? heading.slice(0, 90) + '…' : heading));
    const {mappingReady, bindingAllowed, partial, noun, title, action} = dependencyPresentation(dependency, binding, harness);
    const statuses = node('div', undefined, 'resource-states');
    resourceStatus(statuses, '配置', title);
    const execution = resourceExecution(dependency);
    resourceStatus(statuses, '实际运行', execution === 'NOT_RUN' ? '尚未运行' : readableResourceStatus(execution));
    row.append(statuses);
    row.append(node('p', '下一步：' + action, 'card-note'));
    if (partial) row.append(node('p', '目前只接好部分功能，可先为这些功能配置资源。其余问题见原始记录。', 'card-note'));
    if (!mappingReady || partial) {
      const repair = node('a', '查看分析与修正', 'detail-link');
      repair.href = '/inspect?' + new URLSearchParams({view: 'analyses', harness: harness.id}); row.append(repair);
    }
    if (dependency.configurable === false) {
      const placement = dependency.placement || {};
      row.append(node('p', '文件需要放在指定位置', 'card-note'));
      if (typeof placement === 'string') row.append(node('p', placement, 'resource-path'));
      else {
        row.append(node('p', '实际放置位置：' + (placement.absolute_path || placement.resolved_path || placement.path || '位置未确定'), 'resource-path'));
        detailValue(row, '查看路径来源', placement);
      }
      const required = dependency.required_files || placement.required_files;
      const missing = dependency.missing_files || placement.missing_files || placement.missing;
      if (required?.length) detailValue(row, '所需文件', required);
      if (missing?.length) row.append(node('p', '缺失：' + missing.join('、'), 'card-note message-error'));
      if (dependency.check) detailValue(row, '源库要求位置的最近检查', dependency.check);
      row.append(node('p', '按原位置准备后重新检查；登记另一路径不会改变原加载方式。', 'card-note'));
      const check = node('button', '检查源库要求的位置', 'secondary-button'); check.type = 'button';
      check.disabled = !bindingAllowed || !placement.resolved_path;
      check.onclick = () => resourceAction(check, '/api/harnesses/check-dependency', {harness_id: harness.id, dependency_id: id},
        '已核对源库要求的位置及文件。本次未覆盖路径或加载模型。');
      row.append(check);
    } else if (bindingAllowed) {
      const form = node('form', undefined, 'dependency-binding');
      const select = node('select'); select.required = true; select.setAttribute('aria-label', '为 ' + (dependency.description || id) + ' 选择资源');
      const none = node('option', '选择已登记的' + (dependency.kind === 'service' ? '连接' : '权重')); none.value = ''; select.append(none);
      const records = entries(dependency.kind === 'service' ? resourceState().connections : resourceState().weights);
      if (!records.length) none.textContent = '请先添加' + noun;
      const register = node('a', dependency.kind === 'service' ? '登记／管理服务连接' : '登记／管理权重', 'detail-link');
      register.href = '/configuration?section=' + (dependency.kind === 'service' ? 'services' : 'weights'); row.append(register);
      for (const resource of records) {
        const option = node('option', resource.name || resourceId(resource)); option.value = resourceId(resource); select.append(option);
      }
      select.value = binding?.connection_id || binding?.resource_id || '';
      const button = node('button', '保存并检查', 'secondary-button'); button.type = 'submit';
      select.disabled = !bindingAllowed || !records.length;
      button.disabled = !bindingAllowed || !records.length;
      if (!records.length) row.append(node('p', '尚无已登记资源；先使用上方配置入口登记。', 'card-note'));
      const status = node('span', '', 'form-status'); status.setAttribute('role', 'status');
      form.append(select, button, status);
      form.onsubmit = async event => {
        event.preventDefault(); button.disabled = true; status.textContent = '正在检查接入配置…';
        try {
          const body = {harness_id: harness.id, dependency_id: id};
          body[dependency.kind === 'service' ? 'connection_id' : 'resource_id'] = select.value;
          await api('/api/harnesses/bind-dependency', body);
          showNotice('选择已保存，接入条件已重新检查。尚未实际运行，请查看最新结果。');
          await refresh();
        } catch (error) { status.textContent = error.message; }
        finally { button.disabled = !bindingAllowed || !records.length; }
      };
      row.append(form);
    }
    detailValue(row, '查看原始检查记录', {id, description: dependency.description,
      status: dependency.status, reason: dependency.reason, selection_reason: dependency.selection_reason,
      entry_checks: dependency.entry_checks, entry_ids: dependency.entry_ids,
      resource_origins: dependency.resource_origins,
      binding, mappings: dependency.mappings || dependency.mapping, evidence: dependency.anchor || dependency.evidence,
      original_configuration: dependency.configuration || dependency.config_field,
      read_time: dependency.read_time || dependency.read_at,
      owner_scope: dependency.owner_scope || dependency.owner, lifetime: dependency.lifetime, required: dependency.required});
    if (dependency.last_execution) detailValue(row, '最近原生调用及冻结配置依据', dependency.last_execution);
    detail.append(row);
  }
  detail.append(node('p', '保存配置不会启动模型；正在执行的任务继续使用原配置。', 'card-note'));
  card.append(detail);
}

function hydrateModels() {
  for (const role of ['chat', 'analysis', 'teacher']) {
    const form = $(role + '-model-form');
    const profile = state.models?.[role];
    const selector = form.elements.connection_id;
    connectionOptions(selector);
    if (selector.dataset.selected !== (profile?.connection_id || '')) {
      selector.value = profile?.connection_id || ''; selector.dataset.selected = profile?.connection_id || '';
    }
    if (!form.dataset.hydrated) {
      form.elements.base_url.value = profile?.base_url || '';
      form.elements.model.value = profile?.model || '';
      form.elements.max_output_tokens.value = profile?.declared_max_output_tokens ?? '';
      form.elements.service_version.value = profile?.service_version || '';
      form.dataset.hydrated = 'true';
    }
    form.elements.api_key.placeholder = profile?.key_present ? '留空保留；点击单独保存凭据才会写入' :
      '无鉴权接口可留空；认证方式在服务连接中选择';
    const readiness = $(role + '-model-readiness');
    readiness.textContent = profile?.credential_error || (isReady(profile) ?
      '配置可用 · ' + (profile.key_present ? '凭据已配置' : '认证按所选连接') :
      profile?.configured ? '地址与模型已保存，请检查服务连接的认证方式与凭据。' : '尚未配置');
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
    if (harness.root) card.append(node('p', '本地源码目录：' + harness.root, 'resource-path'));
    if (harness.error) card.append(node('p', textValue(harness.error), 'card-note message-error'));
    const job = (state.analyses || []).find(item => item.id === harness.analysis_id);
    const detached = harness.status === 'DETACHED';
    const busy = analysisSubmitting.has(harness.id) || ['RUNNING', 'QUEUED', 'ANALYZING', 'CANCELLING'].includes(String(job?.status || harness.status).toUpperCase());
    const action = node('button', busy ? '分析中…' : '分析 Harness', 'secondary-button');
    action.type = 'button';
    action.disabled = detached || busy || harness.target_runtime?.status === 'PREPARING' || deploymentSubmitting.has('source/' + harness.id) ||
      sourceUpdating.has(harness.id) || !isReady(state.models?.analysis);
    if (harness.target_runtime?.status === 'PREPARING') action.title = '目标解释器正在准备，完成后再分析';
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
    const activeJob = (state.analyses || []).find(item => item.harness_id === harness.id && ['RUNNING', 'CANCELLING'].includes(item.status));
    const cancel = cancelAnalysisButton(harness, activeJob);
    if (cancel) actions.append(cancel);
    actions.append(inspectLink('analyses', {harness: harness.id}), sourceButton(harness), deleteSourceButton(harness));
    if (detached) card.append(node('p', '已摘除；重新接入会使用已有分析，不重复调用分析模型。', 'card-note'));
    if (!isReady(state.models?.analysis)) actions.append(node('span', '先配置分析模型', 'form-note'));
    card.append(actions);
    sourceEnvironment(card, harness);
    harnessDependencies(card, harness);
    root.append(card);
  }
}

async function saveModel(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button[type="submit"]');
  const status = form.querySelector('.form-status');
  const body = {role: form.dataset.role, base_url: form.elements.base_url.value.trim(), model: form.elements.model.value.trim()};
  body.max_output_tokens = form.elements.max_output_tokens.value ? Number(form.elements.max_output_tokens.value) : null;
  body.service_version = form.elements.service_version.value.trim() || null;
  button.disabled = true;
  status.textContent = '正在保存…';
  try {
    await api('/api/models', body);
    status.textContent = form.elements.api_key.value ? '配置已保存；输入的凭据尚未保存，请点击“单独保存凭据”' : '配置已保存';
    await refresh();
  } catch (error) { status.textContent = error.message; }
  finally { button.disabled = false; }
}

async function selectRoleConnection(event) {
  const button = event.currentTarget;
  const form = button.closest('form');
  const status = form.querySelector('.form-status');
  const connectionId = form.elements.connection_id.value;
  if (!connectionId) { status.textContent = '先选择已保存的命名连接'; return; }
  button.disabled = true;
  try {
    await api('/api/models/select', {role: form.dataset.role, connection_id: connectionId});
    form.elements.api_key.value = '';
    delete form.dataset.hydrated;
    status.textContent = '已选择连接；不会改动其他角色或在途运行';
    await refresh();
  } catch (error) { status.textContent = error.message; }
  finally { button.disabled = false; }
}

async function saveRoleCredential(event) {
  const button = event.currentTarget;
  const form = button.closest('form');
  const status = form.querySelector('.form-status');
  const profile = state.models?.[form.dataset.role];
  if (!profile?.connection_id || profile.base_url !== form.elements.base_url.value.trim() || profile.model !== form.elements.model.value.trim()) {
    status.textContent = '先保存当前地址及模型配置，再为该连接单独保存凭据'; return;
  }
  if (!form.elements.api_key.value) { status.textContent = '输入为空，保留已有凭据'; return; }
  button.disabled = true;
  try {
    await api('/api/credentials', {connection_id: profile.connection_id, action: 'save', value: form.elements.api_key.value});
    form.elements.api_key.value = '';
    status.textContent = '该连接凭据已单独保存'; await refresh();
  } catch (error) { status.textContent = error.message; }
  finally { button.disabled = false; }
}

async function saveConnection(event) {
  event.preventDefault();
  const form = event.currentTarget, status = $('connection-form-status');
  const button = form.querySelector('[type="submit"]'); button.disabled = true;
  try {
    const parameters = JSON.parse(form.elements.parameters.value.trim() || '{}');
    if (!parameters || typeof parameters !== 'object' || Array.isArray(parameters)) throw new Error('非敏感参数需要 JSON 对象');
    const body = {name: form.elements.name.value.trim(), protocol: form.elements.protocol.value,
      base_url: form.elements.base_url.value.trim(), model: form.elements.model.value.trim(),
      service_version: form.elements.service_version.value.trim() || null, parameters,
      capabilities: [...form.querySelectorAll('[name="capability"]:checked')].map(input => input.value),
      auth: {mode: form.elements.auth_mode.value}};
    if (form.elements.id.value) body.id = form.elements.id.value;
    if (body.auth.mode === 'environment') body.auth.env = form.elements.auth_env.value.trim();
    if (body.auth.mode === 'saved') {
      const previous = entries(resourceState().connections).find(item => resourceId(item) === body.id);
      if (previous?.base_url === body.base_url && previous.auth?.credential_ref) body.auth.credential_ref = previous.auth.credential_ref;
    }
    const result = await api('/api/connections', body);
    const id = resourceId(result.connection || result);
    if (id) form.elements.id.value = id;
    form.dataset.savedUrl = body.base_url;
    status.textContent = '配置已保存；未启动服务或发起推理';
    await refresh();
    if (id) {
      selectCredentialConnection(id);
      $('connection-form-title').textContent = '编辑连接 · ' + body.name;
    }
  } catch (error) { status.textContent = error.message; }
  finally { button.disabled = false; }
}

async function saveConnectionCredential(event) {
  event.preventDefault();
  const form = event.currentTarget, status = $('credential-form-status');
  const button = form.querySelector('[type="submit"]');
  if (!form.elements.credential.value) { status.textContent = '输入为空，保留已有凭据'; return; }
  button.disabled = true;
  try {
    await api('/api/credentials', {connection_id: form.elements.connection_id.value, action: 'save', value: form.elements.credential.value});
    form.elements.credential.value = ''; status.textContent = '凭据已单独保存'; await refresh();
  } catch (error) { status.textContent = error.message; }
  finally { button.disabled = false; }
}

async function clearConnectionCredential(event) {
  const button = event.currentTarget, form = $('connection-credential-form'), status = $('credential-form-status');
  if (!form.elements.connection_id.value) { status.textContent = '先选择需要清除凭据的命名连接'; return; }
  button.disabled = true;
  try {
    await api('/api/credentials', {connection_id: form.elements.connection_id.value, action: 'clear'});
    form.elements.credential.value = ''; status.textContent = '凭据已清除；请按原服务要求选择认证方式'; await refresh();
  } catch (error) { status.textContent = error.message; }
  finally { button.disabled = false; }
}

async function saveWeight(event) {
  event.preventDefault();
  const form = event.currentTarget, status = $('weight-form-status');
  const button = form.querySelector('[type="submit"]'); button.disabled = true;
  try {
    const text = form.elements.required_files.value.trim();
    const required = text.startsWith('[') ? JSON.parse(text) : text.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    if (!Array.isArray(required)) throw new Error('必要文件需要每行一个路径或 JSON 列表');
    const body = {name: form.elements.name.value.trim(), path: form.elements.path.value.trim(), kind: form.elements.kind.value,
      model_revision: form.elements.model_revision.value.trim() || null, execution_environment: 'local', required_files: required};
    if (form.elements.id.value) body.id = form.elements.id.value;
    const result = await api('/api/weights', body), id = resourceId(result.resource || result.weight || result);
    if (id) form.elements.id.value = id;
    $('weight-form-title').textContent = '编辑权重 · ' + body.name;
    status.textContent = '路径已登记；点击“检查资源”核对必要文件'; await refresh();
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
    await refreshRevisionDetails();
    await refreshPreparationSummaries();
    connected = true;
    $('connection').textContent = '已连接';
    $('version').textContent = state.version ? '核心 ' + state.version : '';
    document.querySelector('.connection-dot').classList.remove('disconnected');
    hydrateModels();
    hydrateResearch();
    hydrateSceneTasks();
    updateInspectPage();
    renderSection('profile', state.entry_profile, renderProfile, ['entry-profile-summary', 'config-entry-profile']);
    renderSection('connections', resourceState().connections, renderConnections, ['connection-list']);
    renderSection('weights', resourceState().weights, renderWeights, ['weight-list']);
    synchronizeTask();
    renderSection('scheduler', state.scheduler, renderScheduler, ['scheduler-summary']);
    renderSection('research', [state.scheduler?.research, state.units, state.harnesses], renderResearch,
      ['research-readiness', 'research-run-list', 'research-comparison']);
    renderSection('tasks', visibleTasks(), renderTasks, ['task-list']);
    renderSection('units', [visibleUnits(), state.harnesses, selectedSource()], renderUnits, ['unit-list']);
    renderSection('deployments', [state.runtime_groups, state.deployments, state.deployment_backend,
      state.runtime_controller, selectedSource(), [...deploymentPreparation]], renderDeployments, ['deployment-list']);
    renderSection('analyses', [state.analyses, state.harnesses, state.analysis_revisions, state.models?.analysis,
      resourceState(), selectedSource(), [...sourceUpdating], [...revisionSelected], [...revisionDetails], [...revisionErrors], [...revisionBusy],
      [...preparationSummaries], [...preparationErrors]], renderAnalyses, ['analysis-list']);
    renderSection('harnesses', [state.harnesses, state.analyses, state.models?.analysis, resourceState(), [...analysisSubmitting], [...sourceUpdating],
      [...deploymentPreparation]], renderHarnesses, ['harness-list']);
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
$('new-chat').onclick = () => resetChat();
$('toggle-chat-history').onclick = () => {
  $('chat-history').hidden = !$('chat-history').hidden;
  $('toggle-chat-history').setAttribute('aria-expanded', String(!$('chat-history').hidden));
  if (!$('chat-history').hidden) { loadChatHistory(); $('chat-history-search').focus(); }
};
$('chat-history-search').oninput = () => {
  clearTimeout(historySearchTimer); historySearchTimer = setTimeout(loadChatHistory, 180);
};
$('scene-task-selector').onchange = updateComposer;
$('chat-input').oninput = resizeComposer;
$('chat-input').onkeydown = event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!$('send-message').disabled) $('chat-form').requestSubmit();
  }
};
$('chat-model-form').onsubmit = saveModel;
$('analysis-model-form').onsubmit = saveModel;
$('teacher-model-form').onsubmit = saveModel;
for (const button of document.querySelectorAll('.select-role-connection')) button.onclick = selectRoleConnection;
for (const button of document.querySelectorAll('.save-role-credential')) button.onclick = saveRoleCredential;
$('connection-form').onsubmit = saveConnection;
$('connection-invoke-form').onsubmit = invokeConnection;
$('connection-invoke-form').elements.connection_id.onchange = () => {
  $('connection-invoke-status').textContent = ''; $('connection-invoke-result').replaceChildren();
  updateConnectionInvocation();
};
$('connection-invoke-form').elements.operation.onchange = updateConnectionInvocation;
$('connection-form').elements.auth_mode.onchange = connectionAuthChanged;
$('connection-form').elements.base_url.onchange = () => {
  const form = $('connection-form');
  if (form.dataset.savedUrl && form.dataset.savedUrl !== form.elements.base_url.value.trim()) {
    form.elements.auth_mode.value = 'saved'; form.elements.auth_env.value = ''; connectionAuthChanged();
    $('connection-auth-note').textContent = '地址已更改。请为新目标明确选择认证方式；原凭据不会默认转发。';
  }
};
$('connection-new').onclick = () => {
  const form = $('connection-form'); form.reset(); form.elements.id.value = ''; delete form.dataset.savedUrl;
  $('connection-form-title').textContent = '新增命名连接'; $('connection-form-status').textContent = '';
  $('connection-auth-note').textContent = '凭据值使用下方独立表单保存。更换地址后需重新明确认证方式。'; connectionAuthChanged();
};
$('connection-create').onclick = () => {
  $('connection-new').click();
  $('connection-form').scrollIntoView({block: 'start'});
  $('connection-form').elements.name.focus({preventScroll: true});
};
$('connection-credential-form').onsubmit = saveConnectionCredential;
$('connection-credential-form').elements.connection_id.onchange = () => {
  $('connection-credential-form').elements.credential.value = '';
  $('credential-form-status').textContent = '';
};
$('connection-clear-credential').onclick = clearConnectionCredential;
$('weight-form').onsubmit = saveWeight;
$('weight-create').onclick = () => {
  $('weight-new').click(); $('weight-form').scrollIntoView({block: 'start'});
  $('weight-form').elements.name.focus({preventScroll: true});
};
$('weight-new').onclick = () => {
  const form = $('weight-form'); form.reset(); form.elements.id.value = '';
  $('weight-form-title').textContent = '新增权重资源'; $('weight-form-status').textContent = '';
};
$('research-form').onsubmit = saveResearch;
$('research-start').onclick = startResearch;
$('research-form').addEventListener('input', () => { researchDirty = true; });
$('research-teacher-role').onchange = () => hydrateResearchChoices();
$('research-filter').oninput = renderResearch;
$('research-mode-filter').onchange = renderResearch;
$('research-load-saved').onclick = () => {
  const saved = researchState().config;
  loadResearchConfig(saved?.config || saved || blankResearchConfig());
  $('research-form-status').textContent = saved ? '已加载保存的配置。' : '尚无保存配置，请填写真实环境和评价资源。';
};
$('research-criteria').onchange = () => {
  try {
    const criteria = jsonField('research-criteria', '评价资源');
    $('research-prompt').value = criteria.prompt || '';
    $('research-teacher-role').value = criteria.teacher?.role || 'teacher';
    hydrateResearchChoices();
  } catch (error) { $('research-form-status').textContent = error.message; }
};
$('research-scene').onchange = () => {
  try {
    researchDraft.scene = jsonField('research-scene', '场景配置');
    $('research-unit-options').dataset.options = '';
    const selected = new Set(researchDraft.scene.unit_ids || []);
    hydrateResearchChoices(false, selected);
  } catch (error) { $('research-form-status').textContent = error.message; }
};
$('research-export').onclick = () => {
  try { downloadJson(readResearchConfig(), 'seampact-run-config.json'); }
  catch (error) { $('research-form-status').textContent = error.message; }
};
$('research-criteria-export').onclick = () => {
  try { downloadJson(readResearchCriteria(), 'seampact-criterion.json'); }
  catch (error) { $('research-form-status').textContent = error.message; }
};
$('research-import').onchange = async event => {
  try {
    const file = event.target.files[0]; if (!file) return;
    const value = JSON.parse(await file.text());
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('配置文件需要 JSON 对象');
    loadResearchConfig(value.config || value); researchDirty = true;
    $('research-form-status').textContent = '已导入到编辑区；保存或开始运行后生效。';
  } catch (error) { $('research-form-status').textContent = error.message; }
  finally { event.target.value = ''; }
};
$('research-criteria-import').onchange = async event => {
  try {
    const file = event.target.files[0]; if (!file) return;
    const criteria = JSON.parse(await file.text());
    if (!criteria || typeof criteria !== 'object' || Array.isArray(criteria)) throw new Error('评价文件需要 JSON 对象');
    $('research-criteria').value = JSON.stringify(criteria, null, 2);
    $('research-prompt').value = criteria.prompt || '';
    $('research-teacher-role').value = criteria.teacher?.role || 'teacher';
    researchDirty = true; hydrateResearchChoices();
    $('research-form-status').textContent = '已导入完整评价资源；在途运行继续使用原副本。';
  } catch (error) { $('research-form-status').textContent = error.message; }
  finally { event.target.value = ''; }
};
$('open-research-configuration').onclick = () => {
  navigatePage('/configuration?section=research');
};
$('harness-form').onsubmit = addHarness;
$('open-observer').onclick = () => observerOpen(!document.body.classList.contains('observer-open'));
$('close-observer').onclick = () => observerOpen(false);
$('drawer-shade').onclick = () => observerOpen(false);
$('inspect-source').onchange = async event => {
  const query = new URLSearchParams({view: inspectView});
  if (event.target.value) query.set('harness', event.target.value);
  inspectQuery = query;
  history.replaceState(null, '', '/inspect?' + query);
  activePageUrl = '/inspect?' + query;
  renderedSections.clear();
  document.querySelector('.observer-scroll').scrollTop = 0;
  await refresh();
};
document.addEventListener('click', event => {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
  const link = event.target.closest('a[href]');
  if (!link || link.hasAttribute('download') || (link.target && link.target !== '_self')) return;
  const target = new URL(link.href, location.href);
  if (target.origin !== location.origin || !['/', '/inspect', '/configuration'].includes(target.pathname)) return;
  if (target.hash && target.pathname + target.search === activePageUrl) {
    const section = document.getElementById(target.hash.slice(1));
    if (section) { event.preventDefault(); section.scrollIntoView({block: 'start'}); }
    return;
  }
  event.preventDefault();
  navigatePage(target.href);
});
window.addEventListener('popstate', () => navigatePage(location.href, false));

document.addEventListener('keydown', event => {
  if (event.key === 'Escape') observerOpen(false);
});
updateInspectPage();
if (!inspectPage && !configurationPage) restoreChatFromLocation();
refresh();
setInterval(refresh, 1500);
setInterval(updateAnalysisClocks, 1000);
