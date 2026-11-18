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
let chatContextTask = null;
let chatRestoring = false;
let chatLoadVersion = 0;
let historySearchVersion = 0;
let historySearchTimer;
let projects = [];
let projectApplication = null;
let projectApplicationVersion = 0;
let projectApplicationBusy = false;
let projectDemoBusy = false;
let privacyDemoCases = null;
let activeProjectId = 'default';
let projectFilePath = '';
let projectFileVersion = 0;
let projectFileBusy = false;
const preparationSummaries = new Map();
const preparationErrors = new Map();
const analysisSubmitting = new Set();
const sourceUpdateOptions = new Map();
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
const gpuAllocationDrafts = new Map();
const capabilityDrafts = new Map();
const deploymentSubmitting = new Set();
const deploymentPreparation = new Map();
let imageInventory = null;
let imageBusy = false;
let imageError = '';
const imageSelected = new Set();
const imageDraft = {};
let inspectPage = location.pathname === '/inspect';
let configurationPage = location.pathname === '/configuration';
const configNames = {models: '模型用途', services: '服务连接', weights: '本地模型文件', harnesses: '仓库接入'};
const modelRoleLabels = {chat: '对话模型', analysis: '仓库分析模型', teacher: '实验评分模型'};
const initialConfigQuery = new URLSearchParams(location.search);
let configSection = Object.hasOwn(configNames, initialConfigQuery.get('section')) ? initialConfigQuery.get('section') : 'models';
let pendingConfigConnection = configurationPage && configSection === 'services' ? initialConfigQuery.get('connection') : null;
let configurationReturnUrl = '/';
const configurationScroll = new Map();
const observerScroll = new Map();
let activePageUrl = location.pathname + location.search;
let inspectQuery = new URLSearchParams(location.search);
let pendingSectionHash = location.hash;
let pendingRevisionId = inspectQuery.get('revision');
let pendingInspectItem = inspectQuery.get('unit') || inspectQuery.get('task');
let inspectView = inspectQuery.get('view') || 'analyses';
const viewNames = {analyses: '仓库准备', units: '已接入能力', tasks: '任务记录', scheduler: '调度策略', deployments: '能力部署', operations: '资源观察'};
const viewDescriptions = {
  analyses: '跟踪仓库准备进度，查看缺少的模型、文件或其他条件。',
  units: '查看能力的用途、可用状态和输入要求。',
  tasks: '按项目名称和开始时间辨认任务，打开记录查看调用及结果。',
  scheduler: '选择后续任务使用的策略，查看实验和训练结果。',
  deployments: '查看能力所需资源，以及正在运行或等待释放的实例。',
  operations: '采集平台、容器和 GPU 的资源用量。'
};
let operationsLoading = false;
let operationsSnapshot = null;
let monitorSnapshot = null;
let monitorDetail = null;
let monitorSelection = '';
let monitorMetricsSignature = '';
let monitorBusy = false;
let monitorGpuBusy = false;
let monitorGpuHydrated = false;
let monitorGpuDirty = false;
let monitorGpuRevision = 0;
let taskDeleteBusy = false;
let olderTasks = [];
let olderTasksCursor;
let olderTasksBusy = false;
let inspectedTask = null;
let operationsError = '';
let operationsRequestVersion = 0;
let monitorSeriesChoice = '';
if (!viewNames[inspectView]) inspectView = 'analyses';
const renderedSections = new Map();
const detailStates = new Map();
const researchSelected = new Set();
const researchDetails = new Map();
const researchDetailVersions = new Map();
const researchLoading = new Set();
let policySnapshot = null;
let policySelected = new URLSearchParams(location.search).get('policy') || '';
const policyDetails = new Map();
let policyDetailVersion = 0;
let policyLoading = false;
let policyBusy = false;
let policyError = '';
let policyTaskSignature = '';
const learnerNames = {double_dqn: 'Double DQN', ppo: 'PPO', discrete_sac: '离散 SAC', required_only: '仅必需工作', all_applicable: '全部适用工作'};
const researchModeNames = {train: '新训练', freeze: '冻结评价', warmstart: '继续训练', rejudge: '条件重评'};
const terminalStatuses = new Set(['COMPLETED', 'SUCCEEDED', 'FAILED', 'PREPARATION_FAILED', 'CANCELLED', 'INTERRUPTED', 'BLOCKED']);
const instanceStateNames = {
  UNDEPLOYED: '未部署', STARTING: '启动中', READY: '已就绪', DRAINING: '排空中',
  STOPPED: '已停止', FAILED: '失败', UNKNOWN: '状态未知'
};
const statusNames = {
  RUNNING: '运行中', QUEUED: '排队中', WAITING: '等待中', COMPLETED: '已完成',
  SUCCEEDED: '已完成', FAILED: '失败', CANCELLED: '已取消', CANCELLING: '正在取消', INTERRUPTED: '已中断',
  PREPARATION_FAILED: '准备条件失败', READY_TO_RUN: '本轮已准备', CLOSING: '等待收尾', CLEANUP_PENDING: '引用待清理', CLOSED: '本轮已结束',
  READY: '已登记', AVAILABLE: '可用', NEEDS_INPUT: '需补充', PREPARING: '准备中', NOT_PREPARED: '待准备', BLOCKED: '已阻止', PENDING: '待确认',
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
  if (detail.dataset.detailKey) return detail.dataset.detailKey;
  const owner = detail.closest('[data-item-id]')?.dataset.itemId || root.id;
  const scope = detail.closest('[data-detail-key]')?.dataset.detailKey || '';
  const labels = [];
  for (let current = detail; current && current !== root; current = current.parentElement) {
    if (current !== detail && current.dataset.detailKey) break;
    if (current.tagName === 'DETAILS') labels.unshift(current.querySelector(':scope > summary')?.textContent.split(' · ')[0] || '详情');
  }
  return root.id + '/' + scope + '/' + owner + '/' + labels.join('/');
}

function disclosureScope() { return inspectPage ? selectedSource() ? 'source/' + selectedSource() : 'all' : 'overview'; }

function compactText(value, limit = 180) {
  const text = textValue(value).replace(/\s+/g, ' ').trim();
  return text.length > limit ? text.slice(0, limit) + '…' : text;
}

function disclosure(key, title, {status, meta, error, open = false} = {}) {
  const detail = node('details', undefined, 'item-disclosure');
  detail.dataset.detailKey = disclosureScope() + '/' + key;
  // Identity is independent of status, purpose text, position and list length.
  const identity = detail.dataset.detailKey;
  detail.open = detailStates.has(identity) ? detailStates.get(identity) : open;
  detail.addEventListener('toggle', () => { if (detail.isConnected) detailStates.set(identity, detail.open); });
  const summary = node('summary');
  const heading = node('span', undefined, 'disclosure-heading');
  heading.append(node('strong', title));
  if (status) heading.append(pill(status));
  summary.append(heading);
  if (meta) summary.append(node('span', meta, 'disclosure-meta'));
  if (error) summary.append(node('span', compactText(error), 'disclosure-error'));
  detail.append(summary);
  return detail;
}

function foldCard(card, key, title, options) {
  if (!inspectPage) return card;
  const detail = disclosure(key, title, options);
  card.querySelector(':scope > .card-heading')?.remove();
  detail.append(card);
  return detail;
}

function revealSection(section, scroll = true) {
  for (let current = section?.parentElement; current; current = current.parentElement) {
    if (current.tagName === 'DETAILS') {
      current.open = true;
      if (current.dataset.detailKey) detailStates.set(current.dataset.detailKey, true);
    }
  }
  if (scroll) section?.scrollIntoView({block: 'start'});
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
  const sourceGroups = new Set((state.runtime_groups || []).filter(group => group.source_id === source).map(group => group.id));
  const current = new Set((state.tasks || []).map(task => task.id));
  const tasks = [...(state.tasks || []), ...olderTasks.filter(task => !current.has(task.id))];
  const identity = inspectPage && inspectView === 'tasks' ? inspectQuery.get('task') : '';
  if (identity && !tasks.some(task => task.id === identity) && inspectedTask?.id === identity && inspectedTask.record) {
    tasks.push(inspectedTask.record);
  }
  return tasks.filter(task => (!identity || task.id === identity) &&
    (!source || (task.calls || []).some(call => call?.unit?.source_id === source ||
      sourceUnits.has(call?.unit_id) || sourceGroups.has(call?.group_id)) ||
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
  document.querySelector('.rail-caption').textContent = '仓库能力 · ' + (configurationPage ? '配置' : inspectPage ? '详情' : '对话');
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
  for (const section of document.querySelectorAll('.observer-scroll > .panel-section')) {
    section.hidden = inspectPage ? section.dataset.view !== inspectView : !['scheduler', 'tasks'].includes(section.dataset.view);
  }
  for (const link of document.querySelectorAll('.rail-actions a[href^="/inspect"]')) {
    const view = new URL(link.href).searchParams.get('view');
    link.classList.toggle('selected', inspectPage && view === inspectView);
  }
  $('observer-title').textContent = inspectPage ? viewNames[inspectView] : '运行状态';
  $('inspect-description').hidden = !inspectPage;
  $('inspect-description').textContent = inspectView === 'analyses' && state?.analysis_revisions_observation_pending ?
    '后台正在检查来源。列表显示最近保存的结果。' : viewDescriptions[inspectView];
  $('inspect-filter').hidden = !inspectPage || ['scheduler', 'operations'].includes(inspectView);
  if (!inspectPage) return;
  for (const link of document.querySelectorAll('.rail-actions a[href^="/inspect"]')) {
    const view = new URL(link.href).searchParams.get('view');
    const query = new URLSearchParams({view});
    if (selectedSource() && !['scheduler', 'operations'].includes(view)) query.set('harness', selectedSource());
    link.href = '/inspect?' + query;
    link.removeAttribute('target');
    link.classList.toggle('selected', view === inspectView);
  }
  const options = (state?.harnesses || []).map(source => [source.id, source.name || source.address, source.status]);
  const selector = $('inspect-source');
  if (selector.dataset.options !== JSON.stringify(options)) {
    selector.replaceChildren(node('option', '全部仓库'));
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
  pendingSectionHash = target.hash;
  pendingRevisionId = inspectQuery.get('revision');
  pendingInspectItem = inspectQuery.get('unit') || inspectQuery.get('task');
  inspectView = viewNames[inspectQuery.get('view')] ? inspectQuery.get('view') : 'analyses';
  if (inspectView === 'scheduler' && inspectQuery.get('policy')) policySelected = inspectQuery.get('policy');
  configSection = Object.hasOwn(configNames, inspectQuery.get('section')) ? inspectQuery.get('section') : 'models';
  pendingConfigConnection = configurationPage && configSection === 'services' ? inspectQuery.get('connection') : null;
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
  const root = $('entry-profile-summary');
  root.replaceChildren(node('p', '完整仓库生成能力包，由独立实例执行。对话共用流式宿主，可直接回答、调用能力或交付文件。', 'card-note'));
  root.append(node('p', '未配置检查插件时正常交付；应用要求的检查必须在对应操作或交付前完成。', 'card-note'));
  root.append(node('p', '停止交付会请求取消；远端停算仅在引擎确认后记录。', 'card-note'));
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

function renderProjectSelection() {
  const select = $('project-select');
  select.replaceChildren();
  for (const project of projects) {
    if (project.archived && project.id !== activeProjectId) continue;
    const option = node('option', project.name); option.value = project.id;
    select.append(option);
  }
  if (![...select.options].some(option => option.value === activeProjectId)) {
    const option = node('option', activeProjectId === 'default' ? '默认项目' : activeProjectId);
    option.value = activeProjectId; select.append(option);
  }
  select.value = activeProjectId;
  $('project-demo-case').value = '';
  void loadProjectApplication();
}

async function loadPrivacyDemoCases() {
  if (privacyDemoCases !== null) return;
  try {
    const result = await api('/api/experiments/office/privacy');
    privacyDemoCases = result.cases || [];
    const select = $('project-demo-case');
    for (const item of privacyDemoCases) {
      const option = node('option', item.title + (item.variant === 'risk' ? ' · 风险配对' : ' · 正常配对'));
      option.value = item.id; select.append(option);
    }
    updateComposer();
  } catch (error) { $('project-demo-feedback').textContent = error.message; }
}

async function startPrivacyDemo() {
  const caseId = $('project-demo-case').value;
  if (!caseId || projectDemoBusy || pendingTask || chatRestoring || projectFileBusy || projectApplicationBusy ||
      !connected || !isReady(state?.models?.chat)) return;
  const projectId = activeProjectId;
  projectDemoBusy = true; chatRestoring = true; updateComposer();
  $('project-demo-feedback').textContent = '正在建立本项目的模拟任务…';
  try {
    const result = await api('/api/experiments/office/privacy', {project_id: projectId, case_id: caseId, demo: true});
    if (!result.task_id || !result.conversation_id) throw new Error('平台未返回任务和会话标识');
    chatRestoring = false;
    await loadConversation(result.conversation_id, true);
    $('project-demo-feedback').textContent = '模拟任务已发起，交付回执保存在此会话工作区；完整检查与评分见任务详情。';
    await refresh();
  } catch (error) { $('project-demo-feedback').textContent = error.message; }
  finally { projectDemoBusy = false; chatRestoring = false; updateComposer(); }
}

function renderProjectApplication() {
  const root = $('project-library-list');
  const current = projectApplication?.application?.project_id === activeProjectId ? projectApplication : null;
  root.replaceChildren();
  if (!current) { root.append(node('p', '正在读取本项目的库。', 'card-note')); return; }
  const archived = projects.find(project => project.id === activeProjectId)?.archived;
  const libraries = current.libraries || [];
  $('project-application-summary').textContent = '· ' + libraries.filter(item => item.enabled).length + ' 个已启用';
  for (const library of libraries) {
    const row = node('label', undefined, 'project-library-row');
    const checkbox = node('input'); checkbox.type = 'checkbox'; checkbox.checked = library.enabled;
    checkbox.disabled = projectApplicationBusy || archived || !library.project_eligible;
    checkbox.onchange = () => {
      const enabled = libraries.filter(item => item.enabled && item.group_id !== library.group_id).map(item => item.group_id);
      if (checkbox.checked) enabled.push(library.group_id);
      void saveProjectApplication({enabled_groups: enabled});
    };
    const label = node('span'); label.append(node('strong', library.name));
    const status = !library.project_eligible ? '资源属于其他项目' : !library.enabled ? '后续任务停用' :
      library.callable_count + '/' + library.capability_count + ' 项能力可调用';
    label.append(node('small', status + (library.enabled && library.availability?.reason ? ' · ' + library.availability.reason : '')));
    row.append(checkbox, label); root.append(row);
  }
  if (!libraries.length) root.append(node('p', '尚未安装库，可在“来源与能力”中接入。', 'card-note'));
  const candidates = current.application.guard_candidates || [];
  const pending = (current.application.binding_status || []).filter(item => !['ENABLED', 'DISABLED'].includes(item.status));
  const auto = $('project-guards-auto');
  auto.hidden = !Object.keys(current.application.bindings || {}).length;
  auto.disabled = projectApplicationBusy || archived;
  $('project-application-bindings').textContent = '已启用的库提供 ' + candidates.length + ' 项可调度检查。' +
    (pending.length ? ' 未生效的指定检查：' + pending.map(item => item.role).join('、') + '。' : '');
  $('project-libraries-none').disabled = projectApplicationBusy || archived || !libraries.some(item => item.enabled);
  $('project-libraries-refresh').disabled = projectApplicationBusy;
}

async function loadProjectApplication() {
  const version = ++projectApplicationVersion;
  const projectId = activeProjectId;
  try {
    const result = await api('/api/projects/application?project_id=' + encodeURIComponent(projectId));
    if (version !== projectApplicationVersion || projectId !== activeProjectId) return;
    projectApplication = result; renderProjectApplication(); updateComposer();
  } catch (error) {
    if (version !== projectApplicationVersion) return;
    $('project-application-feedback').textContent = error.message;
  }
}

async function saveProjectApplication(body) {
  if (projectApplicationBusy) return;
  const projectId = activeProjectId;
  projectApplicationBusy = true; renderProjectApplication(); updateComposer();
  try {
    const result = await api('/api/projects/application', {project_id: projectId, ...body});
    if (projectId !== activeProjectId) return;
    projectApplication = result;
    $('project-application-feedback').textContent = '已保存，将用于后续任务。';
  } catch (error) { $('project-application-feedback').textContent = error.message; }
  finally { projectApplicationBusy = false; renderProjectApplication(); updateComposer(); }
}

async function loadProjects() {
  try {
    const result = await api('/api/projects');
    projects = result.projects || [];
    renderProjectSelection();
  } catch (error) { showNotice(error.message); }
}

function projectFileParams() {
  const params = new URLSearchParams({project_id: activeProjectId, area: $('project-file-area').value, path: projectFilePath});
  if (activeChatId) params.set('conversation_id', activeChatId);
  return params;
}

function updateProjectFileControls() {
  const workspace = $('project-file-area').value === 'workspace';
  const unavailable = workspace && !activeChatId;
  $('project-file-area').disabled = projectFileBusy;
  $('project-file-up').disabled = projectFileBusy || unavailable || !projectFilePath;
  $('project-file-refresh').disabled = projectFileBusy || unavailable;
  $('project-file-upload-button').disabled = projectFileBusy || unavailable || Boolean(pendingTask);
  $('project-file-upload').disabled = projectFileBusy || unavailable || Boolean(pendingTask);
  $('project-scratch-clear').disabled = projectFileBusy || Boolean(pendingTask);
  $('project-workspace-clear').disabled = projectFileBusy || !activeChatId || Boolean(pendingTask);
  for (const button of document.querySelectorAll('.project-file-delete')) button.disabled = projectFileBusy || Boolean(pendingTask);
}

async function loadProjectFiles() {
  const version = ++projectFileVersion;
  const root = $('project-file-list');
  $('project-file-path').textContent = '当前位置：' + (projectFilePath || '根目录');
  updateProjectFileControls();
  if ($('project-file-area').value === 'workspace' && !activeChatId) {
    root.replaceChildren(node('p', '开始或选择一个会话后可查看工作区。', 'card-note')); return;
  }
  root.replaceChildren(node('p', '正在读取…', 'card-note'));
  try {
    const result = await api('/api/project-files?' + projectFileParams());
    if (version !== projectFileVersion) return;
    root.replaceChildren();
    for (const entry of result.entries || []) {
      const row = node('div', undefined, 'project-file-entry');
      if (entry.type === 'directory' || entry.type === 'dir') {
        const button = node('button', '文件夹：' + entry.name, 'icon-button'); button.type = 'button';
        button.onclick = () => { projectFilePath = entry.path; void loadProjectFiles(); };
        row.append(button);
      } else {
        const link = node('a', entry.name, 'detail-link');
        const params = projectFileParams(); params.set('path', entry.path);
        link.href = '/api/project-file?' + params; link.download = entry.name; row.append(link);
        const remove = node('button', '删除', 'icon-button project-file-delete'); remove.type = 'button';
        remove.setAttribute('aria-label', '删除文件：' + entry.name);
        remove.disabled = projectFileBusy || Boolean(pendingTask);
        remove.onclick = () => { void deleteProjectFile(entry.path); }; row.append(remove);
      }
      root.append(row);
    }
    if (!root.childElementCount) root.append(node('p', '这里还没有文件。', 'card-note'));
    if (result.truncated) root.append(node('p', '文件较多，仅显示部分内容。', 'card-note'));
  } catch (error) { if (version === projectFileVersion) root.replaceChildren(node('p', error.message, 'message-error')); }
}

async function deleteProjectFile(path) {
  if (pendingTask || projectFileBusy) return;
  const params = projectFileParams();
  const description = params.get('area') === 'data' ? '项目资料' : '会话文件';
  if (!window.confirm('删除这份' + description + '？已交付的结果和历史记录保留。')) return;
  projectFileBusy = true; updateComposer();
  try {
    await api('/api/project-files/delete', {project_id: params.get('project_id'), area: params.get('area'),
      ...(params.has('conversation_id') ? {conversation_id: params.get('conversation_id')} : {}), path});
    $('project-file-feedback').textContent = '文件已删除。';
    await loadProjectFiles();
  } catch (error) { $('project-file-feedback').textContent = error.message; }
  finally { projectFileBusy = false; updateComposer(); }
}

async function uploadProjectFile() {
  const file = $('project-file-upload').files[0];
  if (!file) { $('project-file-feedback').textContent = '请先选择文件。'; return; }
  if (file.size > 16 * 1024 * 1024) { $('project-file-feedback').textContent = '文件过大，请选择不超过 16 MB 的文件。'; return; }
  const params = projectFileParams();
  const path = [projectFilePath, file.name].filter(Boolean).join('/');
  projectFileBusy = true; updateComposer();
  try {
    const content = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',')[1]);
      reader.onerror = () => reject(new Error('文件读取失败。'));
      reader.readAsDataURL(file);
    });
    await api('/api/project-files', {project_id: params.get('project_id'), area: params.get('area'),
      ...(params.has('conversation_id') ? {conversation_id: params.get('conversation_id')} : {}),
      path, content_base64: content});
    $('project-file-upload').value = ''; $('project-file-feedback').textContent = '文件已上传。';
    await loadProjectFiles();
  } catch (error) { $('project-file-feedback').textContent = error.message; }
  finally { projectFileBusy = false; updateComposer(); }
}

function resetChat(push = true) {
  if (pendingTask || chatRestoring) return;
  chatLoadVersion += 1;
  activeChatId = null; chatLegacy = false; chatContextTask = null; renderChatContext(); messages.length = 0;
  projectFilePath = ''; if ($('project-files').open) void loadProjectFiles();
  $('conversation').replaceChildren();
  const welcome = node('div', undefined, 'welcome');
  welcome.append(node('h2', '有什么想一起完成的？'), node('p', '发送消息开始新对话；历史会话仍保留。'));
  $('conversation').append(welcome);
  $('chat-title').textContent = '新对话';
  $('chat-input').value = '';
  setChatAddress(null, push); showNotice(''); resizeComposer();
  if (!$('chat-history').hidden) loadChatHistory();
}

async function loadChatHistory(offset = 0) {
  const version = ++historySearchVersion;
  const root = $('chat-history-list');
  try {
    const result = await api('/api/chats?q=' + encodeURIComponent($('chat-history-search').value.trim()) + '&project_id=' + encodeURIComponent(activeProjectId) + '&limit=50&offset=' + offset);
    if (version !== historySearchVersion) return;
    if (offset) root.querySelector('.chat-history-more')?.remove();
    else root.replaceChildren();
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
    if (offset + (result.conversations || []).length < result.total) {
      const more = node('button', '更多会话', 'secondary-button chat-history-more');
      more.type = 'button';
      more.onclick = () => loadChatHistory(offset + 50);
      root.append(more);
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
  if (pendingTask || chatRestoring || projectFileBusy) return;
  const version = ++chatLoadVersion;
  chatRestoring = true; updateComposer();
  try {
    const chat = await api('/api/chats/' + encodeURIComponent(identity));
    if (version !== chatLoadVersion) return;
    activeProjectId = chat.project_id || 'default'; renderProjectSelection();
    activeChatId = chat.id; chatLegacy = chat.legacy; messages.splice(0, messages.length, ...chat.messages);
    projectFilePath = ''; if ($('project-files').open) void loadProjectFiles();
    $('conversation').replaceChildren();
    for (const message of messages) appendMessage(message.role, responseText(message.content));
    const first = messages.find(message => message.role === 'user');
    $('chat-title').textContent = chat.legacy ? '旧对话记录' : first ? responseText(first.content).slice(0, 32) : '对话';
    $('chat-input').value = '';
    const task = chat.task;
    chatContextTask = task; renderChatContext();
    if (task.status === 'RUNNING') {
      const output = appendMessage('assistant', '正在运行…', false); output.item.classList.add('message-pending');
      pendingTask = {id: task.id, output, sceneTaskId: null, partial: ''};
      void followChatEvents(pendingTask);
    } else if (!['COMPLETED', 'SUCCEEDED'].includes(task.status)) {
      const partial = typeof task.partial_response === 'string' ? task.partial_response : '';
      const output = appendMessage('assistant', partial || task.error || statusLabel(task.status), Boolean(partial));
      output.item.classList.add('message-error');
      if (partial) output.meta.textContent = [statusLabel(task.status), '未完成的内容', task.error].filter(Boolean).join(' · ');
    }
    if (task.files?.length) renderDeliveredFiles($('conversation').querySelector('.chat-message.assistant:last-child'), task.files);
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


function updateComposer() {
  const ready = connected && isReady(state?.models?.chat);
  $('send-message').disabled = !ready || Boolean(pendingTask) || chatRestoring || chatLegacy || projectFileBusy || projectApplicationBusy || projectDemoBusy || !$('chat-input').value.trim();
  $('new-chat').disabled = Boolean(pendingTask) || chatRestoring || projectFileBusy || projectDemoBusy;
  $('project-select').disabled = Boolean(pendingTask) || chatRestoring || projectFileBusy || projectApplicationBusy || projectDemoBusy;
  $('create-project').disabled = Boolean(pendingTask) || chatRestoring || projectFileBusy || projectApplicationBusy || projectDemoBusy;
  $('project-demo-case').disabled = Boolean(pendingTask) || chatRestoring || projectFileBusy || projectApplicationBusy || projectDemoBusy;
  $('project-demo-start').disabled = !ready || !$('project-demo-case').value || Boolean(pendingTask) || chatRestoring || projectFileBusy || projectApplicationBusy || projectDemoBusy;
  updateProjectFileControls();
  $('send-message').querySelector('.send-label').textContent = pendingTask ? '运行中' : '发送';
  $('stop-message').hidden = !pendingTask;
  $('stop-message').disabled = !pendingTask?.id || Boolean(pendingTask?.cancelling);
  $('stop-message').textContent = pendingTask?.cancelling ? '正在停止…' : '停止交付';
  $('chat-input').placeholder = '发送消息…';
  $('composer-hint').textContent = chatRestoring ? '正在读取历史会话…' : chatLegacy ? '请新建对话继续。' : 'Enter 发送 · Shift + Enter 换行';
  $('chat-model-name').textContent = isReady(state?.models?.chat) ? state.models.chat.model : '对话模型未配置';
  $('chat-model-name').className = 'status-pill ' + (ready ? 'ready' : 'pending');
  const available = projectApplication?.application?.project_id === activeProjectId ?
    projectApplication.application.eligible_capability_ids.length : 0;
  const hint = !connected ? '平台未连接。' : !ready ? '先在配置中填写对话模型接口。' :
    '对话已就绪 · ' + (available ? available + ' 个能力可按需调用。' : '当前可直接对话。接入仓库后可使用更多能力。');
  $('chat-readiness').textContent = hint;
  if ($('welcome-context')) $('welcome-context').textContent = hint;
}

async function sendMessage(event) {
  event.preventDefault();
  const content = $('chat-input').value.trim();
  if (!content || pendingTask || chatRestoring || chatLegacy || projectFileBusy || projectApplicationBusy || !connected || !isReady(state?.models?.chat)) return;
  messages.push({role: 'user', content}); chatContextTask = null; renderChatContext();
  appendMessage('user', content);
  $('chat-input').value = ''; resizeComposer();
  const output = appendMessage('assistant', '正在提交任务…', false);
  output.item.classList.add('message-pending');
  pendingTask = {id: null, output, sceneTaskId: null, partial: '', files: []};
  updateComposer(); showNotice('');
  try {
    const result = await api('/api/chat', {messages, project_id: activeProjectId,
      tool_access: $('chat-read-only').checked ? 'read_only' : 'project',
      ...(activeChatId ? {conversation_id: activeChatId} : {})});
    if (!result.task_id) throw new Error('平台未返回任务 ID');
    pendingTask.id = result.task_id;
    if (result.conversation_id) {
      activeChatId = result.conversation_id; setChatAddress(activeChatId);
      if ($('project-files').open) void loadProjectFiles();
      $('chat-title').textContent = responseText(messages.find(message => message.role === 'user')?.content || '对话').slice(0, 32);
    }
    setMessageBody(output.body, '正在运行…', false);
    updateComposer();
    void followChatEvents(pendingTask);
    await refresh();
  } catch (error) {
    setMessageBody(output.body, error.message, false);
    output.item.classList.remove('message-pending'); output.item.classList.add('message-error');
    pendingTask = null;
    if (!$('chat-history').hidden) loadChatHistory();
    updateComposer();
  }
}

function renderDeliveredFiles(container, files) {
  if (!container || !Array.isArray(files) || !files.length) return;
  let list = container.querySelector('.delivered-files');
  if (!list) { list = node('div', undefined, 'delivered-files'); (container.querySelector('.message-content') || container).append(list); }
  list.replaceChildren();
  for (const file of files) {
    if (!/^[0-9a-f]{64}$/.test(file.sha256 || '')) continue;
    const link = node('a', file.name || file.path || '下载文件', 'detail-link');
    link.href = '/api/artifacts/' + encodeURIComponent(file.sha256);
    link.download = (file.name || file.path || 'artifact').split(/[\\/]/).pop();
    list.append(link, node('span', file.media_type || '', 'card-meta'));
  }
}

async function followChatEvents(pending) {
  let cursor = 0;
  while (pendingTask === pending) {
    try {
      const batch = await api('/api/chat/' + encodeURIComponent(pending.id) + '/events?after=' + cursor);
      if (pendingTask !== pending) return;
      const nearBottom = $('conversation').scrollHeight - $('conversation').scrollTop - $('conversation').clientHeight < 100;
      for (const event of batch.events || []) {
        if (!Number.isSafeInteger(event.id) || event.id <= cursor) continue;
        cursor = event.id;
        if (event.type === 'reset' || event.type === 'start') pending.partial = typeof event.text === 'string' ? event.text : '';
        else if (event.type === 'delta' && typeof event.text === 'string') pending.partial += event.text;
        else if (event.type === 'file' && event.data?.file) {
          pending.files = [...(pending.files || []), event.data.file]; renderDeliveredFiles(pending.output.item, pending.files);
        }
      }
      setMessageBody(pending.output.body, pending.partial || '正在运行…', Boolean(pending.partial));
      pending.output.meta.textContent = '运行中';
      if (nearBottom) pending.output.item.scrollIntoView({block: 'end', behavior: 'auto'});
      if (batch.done) {
        const task = await api('/api/chat/' + encodeURIComponent(pending.id) + '/task');
        if (pendingTask === pending) synchronizeTaskRecord(task);
        if (pendingTask !== pending) return;
        await new Promise(resolve => setTimeout(resolve, 250));
      }
    } catch (error) {
      // A dropped connection must not resubmit the model request. Resume at the last event.
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  }
}

function synchronizeTask() {
  if (!pendingTask?.id) return;
  const task = (state.tasks || []).find(item => item.id === pendingTask.id);
  if (!task) return;
  synchronizeTaskRecord(task);
}

function synchronizeTaskRecord(task) {
  if (!pendingTask || task.id !== pendingTask.id) return;
  if (!pendingTask.sceneTaskId) { chatContextTask = task; renderChatContext(); }
  const status = String(task.status || '').toUpperCase();
  const {output} = pendingTask;
  // Ordinary in-flight output is owned by the event stream, not the slower state poll.
  if (!pendingTask.sceneTaskId && !terminalStatuses.has(status)) return;
  const success = ['COMPLETED', 'SUCCEEDED'].includes(status);
  const text = success ? responseText(task.response) :
    (pendingTask.sceneTaskId ? responseText(task.response) :
      (typeof task.partial_response === 'string' ? task.partial_response : pendingTask.partial));
  const nearBottom = $('conversation').scrollHeight - $('conversation').scrollTop - $('conversation').clientHeight < 100;
  setMessageBody(output.body, text || (task.error ? textValue(task.error) : statusLabel(task.status) + '…'), Boolean(text));
  const duration = latency(task.latency_ms);
  output.meta.textContent = [statusLabel(task.status), duration].filter(Boolean).join(' · ');
  if (terminalStatuses.has(status)) {
    output.item.classList.remove('message-pending');
    if (success) {
      if (text) {
        if (!pendingTask.sceneTaskId) messages.push({role: 'assistant', content: text});
      } else setMessageBody(output.body, task.files?.length ? '文件已生成，可在下方下载。' : '任务已完成，平台未返回对话内容。', false);
    } else {
      output.item.classList.add('message-error');
      if (text) output.meta.textContent += ' · 未完成的内容';
      if (text && task.error) output.meta.textContent += ' · ' + textValue(task.error);
    }
    if (task.files?.length) renderDeliveredFiles(output.item, task.files);
    pendingTask = null;
    updateComposer();
    if (!$('chat-history').hidden) loadChatHistory();
  }
  if (nearBottom) output.item.scrollIntoView({block: 'end', behavior: 'auto'});
}

function renderScheduler() {
  const execution = state.scheduler?.execution, root = $('scheduler-summary'), badge = $('scheduler-status');
  const error = execution?.controller_error || execution?.policy_error;
  root.replaceChildren();
  badge.textContent = !execution ? '状态未知' : error ? '调度异常' : '运行中';
  badge.className = 'status-pill ' + (!execution ? 'pending' : error ? 'error' : 'ready');
  if (!execution) { empty(root, '尚未取得执行状态。'); return; }
  const policy = execution.policy_error ? '策略不可用' : state.scheduler?.policies?.active?.name ||
    (execution.config?.policy?.kind === 'ppo' ? '已训练策略（PPO）' : '规则策略');
  root.append(node('p', policy, 'scheduler-current-name'));
  const counts = [[execution.active_tasks, '项任务进行中'], [execution.queued_calls, '项调用排队'],
    [execution.running_calls, '项调用执行中']].filter(([count]) => Number.isFinite(count));
  if (counts.length) root.append(node('p', counts.map(([count, label]) => count + ' ' + label).join(' · '), 'card-note'));
  if (error) root.append(node('p', textValue(error), 'card-note message-error'));
  if (inspectPage) {
    const diagnostics = disclosure('scheduler-diagnostics', '调度诊断');
    detailValue(diagnostics, '任务与成本统计', execution.objective);
    detailValue(diagnostics, '当前调度参数', execution.config);
    detailValue(diagnostics, '近期调度决策', execution.recent_decisions);
    for (const limitation of execution.limitations || []) diagnostics.append(node('p', limitation, 'card-note'));
    root.append(diagnostics);
  }
}

function policyVersionName(id) {
  const version = (policySnapshot?.versions || []).find(item => item.id === id);
  return version?.name || '未命名策略';
}

function policyNumber(value, digits = 3) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits: digits}) : '未取得';
}

function policyDate(value) {
  if (!value) return '';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false});
}

function policyScopeLabel(metrics) {
  return {training: '训练集结果', frozen_development: '冻结开发集评价结果'}[metrics?.scope] ||
    (metrics?.arrivals != null ? '原实验结果 · 测量范围未登记' : '尚无测量结果');
}

function policyMetrics(metrics = {}) {
  const root = node('dl', undefined, 'policy-metrics');
  for (const [label, key, unit, divisor] of [['业务质量', 'quality', '', 1], ['真实时延', 'latency_s', '秒', 1],
    ['内存占用', 'ram_byte_s', 'GiB·秒', 2 ** 30], ['显存占用', 'vram_byte_s', 'GiB·秒', 2 ** 30], ['Token 用量', 'api_tokens', 'token', 1]]) {
    const cell = node('div'), value = metrics?.[key];
    cell.append(node('dt', label), node('dd', policyNumber(typeof value === 'number' ? value / divisor : null) +
      (typeof value === 'number' && Number.isFinite(value) && unit ? ' ' + unit : ''))); root.append(cell);
  }
  return root;
}

function policyStatus(version) {
  if (version.id === policySnapshot?.active?.id) return 'current';
  return version.compatible === false || version.error ? 'failed' : 'ready';
}

function policyButton(label, action, disabled = false) {
  const button = node('button', label, 'secondary-button'); button.type = 'button';
  button.disabled = policyBusy || disabled; button.onclick = action; return button;
}

function policyOptions(id, rows, placeholder) {
  const select = $(id), signature = JSON.stringify(rows.map(row => [row.id, row.name]));
  if (select.dataset.options === signature) return;
  const previous = select.value;
  const blank = node('option', placeholder); blank.value = ''; select.replaceChildren(blank);
  for (const row of rows) { const option = node('option', row.name || row.id); option.value = row.id; select.append(option); }
  if (previous && !rows.some(row => row.id === previous)) {
    const option = node('option', previous + '（当前不可用）'); option.value = previous; option.disabled = true; select.append(option);
  }
  select.value = previous; select.dataset.options = signature;
}

function renderPolicyTrainingForm() {
  const snapshot = policySnapshot || {}, mode = $('policy-train-mode').value;
  policyOptions('policy-train-base', (snapshot.versions || []).filter(item => item.compatible !== false && item.kind === 'ppo'), '请选择基础版本');
  policyOptions('policy-train-recipe', snapshot.recipes || [], '请选择已登记方案');
  const base = $('policy-train-base'); base.disabled = mode === 'fresh' || policyBusy; base.required = mode !== 'fresh';
  const recipe = (snapshot.recipes || []).find(item => item.id === $('policy-train-recipe').value);
  const count = recipe?.training_tasks;
  $('policy-recipe-summary').textContent = recipe ? (recipe.name || recipe.id) + ' · ' +
    (Array.isArray(count) ? count.length : count ?? '未取得') + ' 项训练任务' + (recipe.project_id ? ' · 项目 ' + recipe.project_id : '') :
    '先选择实验方案。没有可用方案时，可从已完成实验导入。';
  $('policy-training-help').textContent = mode === 'resume' ? '继续训练沿用基础版本的训练状态与参数。' :
    mode === 'branch' ? '继承基础版本策略，并按填写的参数创建独立分支。' : '按所选实验方案初始化新策略。';
  for (const input of $('policy-training-fields').querySelectorAll('input')) input.disabled = policyBusy || mode === 'resume';
  const trainingActive = (snapshot.jobs || []).some(job => ['QUEUED', 'STARTING', 'RUNNING', 'PAUSING', 'REGISTERING'].includes(job.status));
  $('policy-train-submit').disabled = policyBusy || !(snapshot.recipes || []).length || trainingActive || snapshot.busy?.can_switch === false;
  if (trainingActive) $('policy-training-help').textContent += ' 另一个分支正在训练，暂停并完成收尾后可开始新分支。';
  $('policy-save-submit').disabled = policyBusy || snapshot.busy?.can_switch === false;
  $('policy-discover').disabled = policyBusy;
  $('policy-import-submit').disabled = policyBusy;
  $('policy-refresh').disabled = policyLoading || policyBusy;
}

function renderPolicyVersions(snapshot) {
  if (snapshot) policySnapshot = snapshot;
  const data = policySnapshot || {}, current = $('policy-current'); current.replaceChildren();
  const active = data.active;
  current.append(node('strong', active ? '后续任务使用 ' + (active.name || '未命名策略') : '当前使用默认调度策略'));
  current.append(node('p', data.busy?.can_switch === false ? (data.busy.reason || '当前仍有任务在执行，完成后可切换策略。') :
    '切换后，后续新任务使用所选策略。已开始任务保留原来的策略记录。', 'card-note'));
  $('policy-feedback').textContent = policyError;
  const root = $('policy-version-list'); root.replaceChildren();
  const filter = $('policy-filter').value.trim().toLowerCase(), status = $('policy-status-filter').value;
  const versions = (data.versions || []).filter(version => (!filter || [version.name, version.id, version.notes,
    version.parent_id].filter(Boolean).join(' ').toLowerCase().includes(filter)) && (!status || policyStatus(version) === status));
  if (!versions.length) empty(root, (data.versions || []).length ? '没有符合筛选条件的版本。' : '还没有策略版本。可保存当前策略，或导入已完成实验。');
  for (const version of versions) {
    const card = node('article', undefined, 'status-card policy-version' + (version.id === policySelected ? ' selected' : ''));
    card.dataset.policyId = version.id;
    const heading = node('div', undefined, 'card-heading'), activeVersion = version.id === active?.id;
    heading.append(node('strong', version.name || '未命名策略'));
    if (activeVersion) heading.append(node('span', '当前使用', 'status-pill ready'));
    else if (version.compatible === false) heading.append(node('span', '暂不可用', 'status-pill error'));
    card.append(heading);
    const origins = {current: '保存当前策略', office_training: '导入已完成实验', training_checkpoint: '训练结算版本', manual: '手动登记'};
    const origin = origins[version.source?.kind];
    card.append(node('p', [version.kind === 'ppo' ? '已训练策略' : version.kind === 'rule' ? '规则策略' : '调度策略',
      origin, policyDate(version.created_at)].filter(Boolean).join(' · '), 'card-meta'));
    if (version.parent_id) card.append(node('p', '源自：' + policyVersionName(version.parent_id), 'policy-lineage'));
    const children = (data.versions || []).filter(item => item.parent_id === version.id);
    if (children.length) card.append(node('p', '派生分支：' + children.map(item => item.name || '未命名策略').join('、'), 'policy-lineage'));
    if (version.notes) card.append(node('p', version.notes, 'card-note'));
    if (version.metrics) {
      card.append(node('p', policyScopeLabel(version.metrics), 'policy-evidence'), policyMetrics(version.metrics));
      if (version.metrics.arrivals != null) card.append(node('p', '每项任务的平均值 · 测量 ' + policyNumber(version.metrics.arrivals, 0) + ' 项' +
        (version.metrics.delivered != null ? ' · 交付 ' + policyNumber(version.metrics.delivered, 0) + ' 项' : '') +
        (version.metrics.complete === false ? ' · 部分指标尚未取得' : ''), 'card-meta'));
    }
    if (version.error) card.append(node('p', version.error, 'message-error'));
    const actions = node('div', undefined, 'form-actions');
    actions.append(policyButton('查看表现和任务', () => { void selectPolicyDetail(version.id); }),
      policyButton(activeVersion ? '已用于后续任务' : '用于后续任务', () => { void selectPolicyForTasks(version.id); },
        activeVersion || version.compatible === false || data.busy?.can_switch === false));
    card.append(actions);
    if (version.kind === 'ppo' && version.compatible !== false) {
      const training = node('details');
      training.append(node('summary', '以此版本训练'));
      const options = node('div', undefined, 'form-actions');
      options.append(policyButton('沿用参数继续训练', () => preparePolicyBranch(version.id, 'resume')),
        policyButton('修改参数后训练', () => preparePolicyBranch(version.id, 'branch')));
      training.append(options);
      card.append(training);
    }
    root.append(card);
  }
  renderPolicyTrainingForm(); renderPolicyTrainingJobs(); renderPolicyDetail();
}

async function refreshPolicies() {
  if (policyLoading) return;
  policyLoading = true; if ($('policy-refresh')) $('policy-refresh').disabled = true;
  try { policySnapshot = await api('/api/policies'); policyError = ''; renderPolicyVersions(); return true; }
  catch (error) { policyError = '策略版本未能更新：' + error.message; $('policy-feedback').textContent = policyError; return false; }
  finally { policyLoading = false; if ($('policy-refresh')) $('policy-refresh').disabled = policyBusy; }
}

async function policyAction(path, body, success) {
  if (policyBusy) return null;
  policyBusy = true; policyError = '正在处理…'; renderPolicyVersions();
  try {
    const result = await api(path, body);
    const errors = Array.isArray(result?.errors) ? result.errors : [];
    const notice = errors.length ? success + ' ' + errors.length + ' 项记录未能导入：' +
      errors.slice(0, 3).map(item => item.error || '缺少可验证工件').join('；') : success;
    const updated = await refreshPolicies(); policyError = updated ? notice : notice + ' ' + policyError; return result;
  } catch (error) { policyError = error.message; return null; }
  finally { policyBusy = false; renderPolicyVersions(); }
}

async function selectPolicyForTasks(id) {
  const version = (policySnapshot?.versions || []).find(item => item.id === id);
  if (!version || version.compatible === false || policySnapshot?.busy?.can_switch === false) return;
  await policyAction('/api/policies/select', {version_id: id}, '后续任务已切换到“' + (version.name || id) + '”。');
}

async function savePolicyVersion(event) {
  event.preventDefault(); const name = $('policy-save-name').value.trim();
  if (!name) { policyError = '请填写版本名称。'; renderPolicyVersions(); return; }
  const result = await policyAction('/api/policies/save', {name, notes: $('policy-save-notes').value.trim()}, '当前策略已保存。');
  const id = result?.version?.id || result?.id; if (id) await selectPolicyDetail(id);
}

async function importPolicyVersion(event) {
  event.preventDefault(); const protocol = $('policy-import-protocol').value.trim();
  try {
    if (!protocol) throw new Error('请填写已有冻结实验方案的服务器路径。');
    const body = {protocol}, name = $('policy-import-name').value.trim(), grader = $('policy-import-grader').value.trim();
    if (name) body.name = name;
    if (grader) {
      body.grader_command = JSON.parse(grader);
      if (!Array.isArray(body.grader_command) || !body.grader_command.length ||
          body.grader_command.some(value => typeof value !== 'string' || !value.trim())) throw new Error('外部评分命令需要非空的 JSON 字符串参数数组。');
    }
    await policyAction('/api/policies/import', body, '实验方案已登记，可用的已完成策略已导入。');
  } catch (error) { policyError = '导入参数有误：' + error.message; renderPolicyVersions(); }
}

function preparePolicyBranch(id, mode) {
  const version = (policySnapshot?.versions || []).find(item => item.id === id); if (!version) return;
  $('policy-train-mode').value = mode; $('policy-train-base').value = id;
  if (version.recipe_id) $('policy-train-recipe').value = version.recipe_id;
  if (!$('policy-train-name').value.trim()) $('policy-train-name').value = (version.name || id).slice(0, 72) + (mode === 'resume' ? ' · 续训' : ' · 新分支');
  renderPolicyTrainingForm(); $('policy-training-editor').open = true;
  $('policy-training-editor').scrollIntoView({block: 'nearest', behavior: 'smooth'}); $('policy-train-name').focus({preventScroll: true});
}

function policyNumericInput(id, label, options = {}) {
  const raw = $(id).value.trim(); if (!raw) return undefined;
  const value = Number(raw);
  if (!Number.isFinite(value) || options.integer && !Number.isInteger(value) || options.positive && value <= 0 || options.nonnegative && value < 0)
    throw new Error(label + (options.integer ? '需要整数' : '需要有效数值') + (options.positive ? '，且大于零。' : options.nonnegative ? '，且不能小于零。' : '。'));
  return value;
}

function policyTrainingRequest() {
  const name = $('policy-train-name').value.trim(), mode = $('policy-train-mode').value, recipe = $('policy-train-recipe').value;
  if (!name) throw new Error('请填写分支名称。');
  if (!['fresh', 'resume', 'branch'].includes(mode)) throw new Error('请选择训练方式。');
  if (!(policySnapshot?.recipes || []).some(item => item.id === recipe)) throw new Error('请选择可用的实验方案。');
  const body = {name, mode, recipe_id: recipe};
  if (mode !== 'fresh') {
    const parent = (policySnapshot?.versions || []).find(item => item.id === $('policy-train-base').value);
    if (!parent || parent.compatible === false || parent.kind !== 'ppo') throw new Error('请选择可用的已训练基础版本。'); body.parent_id = parent.id;
  }
  if (mode === 'resume') return body;
  const seed = policyNumericInput('policy-train-seed', '随机种子', {integer: true});
  const interval = policyNumericInput('policy-train-update', '更新间隔', {integer: true, positive: true});
  if (seed !== undefined) body.seed = seed; if (interval !== undefined) body.update_after_tasks = interval;
  const rate = policyNumericInput('policy-train-rate', '学习率', {positive: true});
  if (rate !== undefined) body.learner_overrides = {learning_rate: rate};
  const objective = {}, preference = {};
  for (const [key, label] of [['quality', '质量偏好'], ['latency', '时延偏好'], ['ram', '内存偏好'], ['vram', '显存偏好'], ['api', 'Token 偏好']]) {
    const value = policyNumericInput('policy-pref-' + key, label, {positive: true}); if (value !== undefined) preference[key] = value;
  }
  if (Object.keys(preference).length) {
    if (Object.keys(preference).length !== 5) throw new Error('修改五维偏好时请填写全部五项，或全部留空以继承方案。');
    if (Math.abs(Object.values(preference).reduce((sum, value) => sum + value, 0) - 1) > 1e-9) throw new Error('五维偏好之和需要等于 1。'); objective.preference = preference;
  }
  const smoothing = policyNumericInput('policy-train-smoothing', '偏好平滑系数', {positive: true});
  if (smoothing !== undefined) objective.smoothing = smoothing;
  if (Object.keys(objective).length) body.objective_overrides = objective;
  return body;
}

async function startPolicyTraining(event) {
  event.preventDefault();
  try {
    const body = policyTrainingRequest();
    await policyAction('/api/policies/train', body, '训练已开始。进度与结果版本将在下方更新。');
  } catch (error) { policyError = error.message; renderPolicyVersions(); }
}

function renderPolicyTrainingJobs() {
  const root = $('policy-training-list'); root.replaceChildren(); const jobs = policySnapshot?.jobs || [];
  if (!jobs.length) empty(root, '尚无手动发起的训练。');
  const statuses = {QUEUED: '等待开始', STARTING: '正在准备训练', REGISTERING: '正在保存结果版本', RUNNING: '训练中', PAUSING: '正在收尾，准备暂停', PAUSED: '已暂停', COMPLETE: '已完成', COMPLETED: '已完成', FAILED: '训练失败', INTERRUPTED: '训练中断'};
  for (const job of jobs) {
    const card = node('article', undefined, 'status-card'), head = node('div', undefined, 'card-heading');
    head.append(node('strong', job.name || job.id), node('span', statuses[job.status] || job.status, 'status-pill ' +
      (job.status === 'RUNNING' ? 'running' : job.status === 'FAILED' ? 'error' : job.status === 'COMPLETE' ? 'ready' : 'pending'))); card.append(head);
    const progress = {admitted: job.next_index, finished: job.completed_tasks, total: job.planned_tasks, updates: job.actual_updates, ...job.progress};
    if (progress.total == null) progress.total = progress.planned;
    card.append(node('p', '已完成 ' + policyNumber(progress.finished, 0) + ' / ' + policyNumber(progress.total, 0) +
      ' 项 · 已进入 ' + policyNumber(progress.admitted, 0) + ' 项 · 策略更新 ' + policyNumber(progress.updates, 0) + ' 次', 'card-meta'));
    if (typeof progress.total === 'number' && progress.total > 0 && typeof progress.finished === 'number') {
      const bar = node('progress'); bar.max = progress.total; bar.value = progress.finished;
      bar.setAttribute('aria-label', '训练已完成 ' + progress.finished + ' / ' + progress.total); card.append(bar);
    }
    if (job.parent_id) card.append(node('p', '基础版本：' + policyVersionName(job.parent_id), 'policy-lineage'));
    if (job.error) card.append(node('p', job.error, 'message-error'));
    if (job.cleanup_error) card.append(node('p', '收尾未完成：' + job.cleanup_error, 'message-error'));
    if (job.restore_error) card.append(node('p', '策略恢复未完成：' + job.restore_error, 'message-error'));
    const actions = node('div', undefined, 'form-actions');
    if (['QUEUED', 'STARTING', 'RUNNING', 'PAUSING'].includes(job.status)) actions.append(policyButton(job.status === 'PAUSING' ? '等待当前任务收尾' : '暂停训练',
      () => { void policyAction('/api/policies/training/pause', {job_id: job.id}, '已请求暂停，请等待当前任务或训练区间完成。'); }, job.status === 'PAUSING'));
    if (job.status === 'PAUSED') actions.append(policyButton('恢复训练',
      () => { void policyAction('/api/policies/training/resume', {job_id: job.id}, '训练已恢复。'); }, job.resumable === false));
    if (job.cleanup_status === 'REQUIRES_DRAIN' || job.restore_status === 'REQUIRES_SELECTION') {
      const otherTrainingActive = jobs.some(other => other.id !== job.id && ['QUEUED', 'STARTING', 'RUNNING', 'PAUSING', 'REGISTERING'].includes(other.status));
      actions.append(policyButton('重新收尾', () => {
        void policyAction('/api/policies/training/cleanup', {job_id: job.id}, '已重新检查收尾与策略恢复状态，请查看本训练记录。');
      }, otherTrainingActive));
      card.append(node('p', otherTrainingActive ? '其他训练仍在进行，结束或暂停并完成收尾后可重新处理本记录。' :
        '重新收尾只等待已接入任务与实例释放，并恢复原推理策略。训练的失败或中断状态会保留。', 'card-note'));
    }
    const resultId = job.result_version_id || job.version_id;
    if (resultId) actions.append(policyButton('查看结果版本', () => { void selectPolicyDetail(resultId); }));
    card.append(actions); root.append(card);
  }
}

async function selectPolicyDetail(id, reveal = true) {
  policySelected = id; const request = ++policyDetailVersion;
  for (const card of $('policy-version-list').children) card.className = 'status-card policy-version' + (card.dataset.policyId === id ? ' selected' : '');
  renderPolicyDetail();
  try {
    const detail = await api('/api/policies/' + encodeURIComponent(id));
    if (request !== policyDetailVersion || id !== policySelected) return;
    policyDetails.set(id, detail); renderPolicyDetail();
    if (reveal) $('policy-version-detail').scrollIntoView({block: 'nearest', behavior: 'smooth'});
  } catch (error) {
    if (request === policyDetailVersion) { policyDetails.set(id, {error: error.message}); renderPolicyDetail(); }
  }
}

function renderPolicyDetail() {
  const root = $('policy-version-detail'), expanded = root.querySelector?.('details')?.open || false;
  root.replaceChildren(); if (!policySelected) return;
  const data = policyDetails.get(policySelected), heading = node('div', undefined, 'card-heading');
  heading.append(node('h4', '版本详情 · ' + policyVersionName(policySelected)), policyButton('收起详情', () => {
    policySelected = ''; policyDetailVersion++;
    for (const card of $('policy-version-list').children) card.className = 'status-card policy-version'; renderPolicyDetail(); })); root.append(heading);
  if (!data) { root.append(node('p', '正在读取版本与关联任务…', 'card-note')); return; }
  if (data.error) { root.append(node('p', data.error, 'message-error'), policyButton('重试读取', () => { void selectPolicyDetail(policySelected); })); return; }
  const version = data.version || {};
  if (version.notes) root.append(node('p', version.notes, 'card-note'));
  root.append(node('p', policyScopeLabel(version.metrics), 'policy-evidence'), policyMetrics(version.metrics));
  if (version.parent_id) root.append(policyButton('查看基础版本：' + policyVersionName(version.parent_id), () => { void selectPolicyDetail(version.parent_id); }));
  const children = (policySnapshot?.versions || []).filter(item => item.parent_id === policySelected);
  for (const child of children) root.append(policyButton('查看分支：' + (child.name || child.id), () => { void selectPolicyDetail(child.id); }));
  if (version.kind === 'rule') root.append(node('p', '规则策略，不包含神经网络训练状态。', 'card-meta'));
  else if (version.training?.tasks != null || version.training?.updates != null) root.append(node('p', '训练任务 ' + policyNumber(version.training.tasks, 0) + ' 项 · 策略更新 ' + policyNumber(version.training.updates, 0) + ' 次', 'card-meta'));
  const config = node('details'); config.open = expanded; config.append(node('summary', '配置与版本身份'));
  for (const [label, value] of [['版本编号', version.id || policySelected], ['实验方案编号', version.recipe_id],
    ['来源实验编号', version.source?.run_id], ['创建时间', policyDate(version.created_at)],
    ['策略文件 SHA-256（用于核对文件内容）', version.checkpoint_sha256]]) {
    if (value != null && value !== '') config.append(node('p', label + ' · ' + value, 'card-meta'));
  }
  const learner = version.learner_config || version.config?.policy?.learner_config || {};
  const known = {learning_rate: '学习率', batch_size: '每批样本', epochs: '更新轮数', rollout_steps: '更新区间步数',
    entropy_coefficient: '探索系数', clip_ratio: '更新裁剪比例'};
  for (const [key, label] of Object.entries(known)) if (learner[key] != null) config.append(node('p', label + '：' + learner[key], 'card-meta'));
  const objective = version.config?.objective;
  if (objective?.pareto?.preference) config.append(node('p', '训练偏好（质量 / 时延 / 内存 / 显存 / Token）：' +
    ['quality', 'latency', 'ram', 'vram', 'api'].map(key => policyNumber(objective.pareto.preference[key])).join(' / '), 'card-meta'));
  if (objective?.pareto?.smoothing != null) config.append(node('p', '偏好平滑系数：' + policyNumber(objective.pareto.smoothing), 'card-meta'));
  if (objective?.scales) config.append(node('p', '冻结资源尺度（秒 / 内存字节·秒 / 显存字节·秒 / Token）：' +
    ['latency_s', 'ram_byte_s', 'vram_byte_s', 'api_tokens'].map(key => policyNumber(objective.scales[key])).join(' / '), 'card-meta'));
  root.append(config, node('h4', '关联任务与结果'));
  const tasks = data.tasks || [];
  if (!tasks.length) root.append(node('p', '尚无该版本的已记录任务。', 'empty-note'));
  for (const task of tasks) {
    const row = node('article', undefined, 'policy-task');
    const relation = {source_training: '来源实验 · 训练任务', frozen_validation: '冻结开发集评价任务'}[task.relation];
    const link = inspectLink('tasks', {task: task.id});
    link.textContent = [relation || '关联任务', policyDate(task.started_at || task.created_at)].filter(Boolean).join(' · ');
    row.append(link, node('span', statusLabel(task.status), 'card-meta'));
    if (task.metrics) row.append(policyMetrics(task.metrics));
    else row.append(node('p', '业务质量 ' + policyNumber(task.quality) + ' · 时延 ' + policyNumber(typeof task.latency_ms === 'number' ? task.latency_ms / 1000 : null) + ' 秒' +
      (typeof task.delivered === 'boolean' ? task.delivered ? ' · 已交付' : ' · 未交付' : ''), 'card-meta'));
    row.append(policyButton('观察决策与结果', () => showTaskActivity(task.id))); root.append(row);
  }
}

function callDetails(calls) {
  const values = entries(calls);
  if (!values.length) return null;
  const detail = node('details', undefined, 'call-list');
  detail.append(node('summary', '能力调用 · ' + values.length));
  for (const call of values) {
    const row = node('div', undefined, 'call-row');
    if (typeof call === 'string') detailValue(row, '早期调用记录', call);
    else {
      row.append(node('strong', call.unit?.name || call.name || '能力调用'));
      if (call.status || call.physical) row.append(pill(call.status || call.physical));
      if (latency(call.latency_ms)) row.append(node('span', latency(call.latency_ms), 'card-meta'));
      detailValue(row, '调用原始记录', call);
    }
    detail.append(row);
  }
  return detail;
}

function taskDisplayName(task) {
  if (task.origin === 'EXPLICIT_TASK_PLAN') return '接口执行任务';
  if (task.experiment?.arm === 'office_privacy_demo') return '模拟办公任务';
  if (task.experiment) return '实验任务';
  return task.conversation_id || ['conversation', 'harness_execution'].includes(task.phase) ? '对话任务' : '任务';
}

function projectDisplayName(id) {
  return (state?.projects || []).find(project => project.id === id)?.name ||
    (id === 'default' ? '默认项目' : id ? '未命名项目' : '');
}

function showTaskActivity(taskId, task = null) {
  const dialog = node('dialog', undefined, 'task-activity-dialog');
  const body = node('div');
  const decisions = node('details', undefined, 'observer-disclosure');
  const identifiers = node('details', undefined, 'observer-disclosure');
  body.setAttribute('aria-live', 'polite');
  const refresh = node('button', '刷新记录', 'secondary-button');
  const close = node('button', '关闭', 'secondary-button');
  const feedback = node('p', '', 'card-note'); feedback.setAttribute('role', 'status');
  let disposed = false, loading = false, timer = null, active = true;
  const ms = value => Number.isFinite(value) ? value.toFixed(0) + ' ms' : '未知';
  const memory = value => Number.isFinite(value) ? (value / 2 ** 30).toFixed(2) + ' GiB·s' : '未取得';
  async function load() {
    if (disposed || loading) return;
    clearTimeout(timer); loading = true;
    refresh.disabled = true;
    try {
      const data = await api('/api/tasks/' + encodeURIComponent(taskId) + '/activity');
      if (disposed) return;
      active = !['COMPLETED', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED', 'STOPPED', 'TIMEOUT', 'TIMED_OUT', 'CANCELED', 'BLOCKED'].includes(data.status);
      feedback.textContent = active ? '任务运行中，每两秒更新记录。' : '任务已结束，显示已保存记录。';
      feedback.className = 'card-note';
      body.replaceChildren(node('p', [projectDisplayName(data.project_id), policyDate(task?.started_at || task?.created_at),
        statusLabel(data.status)].filter(Boolean).join(' · ')));
      const policy = data.policy_version;
      body.append(node('h3', '本次任务使用的策略'));
      if (policy) {
        body.append(node('p', policy.name || '未命名策略', 'card-note'));
        if (policy.id) {
          const link = node('a', '查看策略版本', 'detail-link');
          link.href = '/inspect?' + new URLSearchParams({view: 'scheduler', policy: policy.id}); link.onclick = () => dialog.close(); body.append(link);
        }
        if (typeof policy.training === 'boolean') body.append(node('p', policy.training ? '专门训练任务' : '任务执行时保留的策略版本', 'card-meta'));
      } else body.append(node('p', '本记录未保存已登记策略身份。', 'card-meta'));
      if (data.metrics) body.append(node('h3', '本次任务的五维结果'), policyMetrics(data.metrics),
        node('p', '内存与显存按下方关联实例记录。共享实例的整个生命周期占用不单独归给本任务。', 'card-note'));
      decisions.replaceChildren(node('summary', '调度决策时间线 · ' + (data.decisions || []).length + ' 条'));
      const decisionContent = node('div', undefined, 'observer-disclosure-body');
      const timeline = node('ol', undefined, 'policy-timeline');
      const kinds = {continue: '继续当前任务', run_hook: '执行检查', dispatch: '派发能力调用', prewarm: '提前准备实例', add_replica: '增加可用实例',
        drain_stop: '等待调用收尾并释放实例', retain: '保留实例', skip: '跳过可选检查', hook: '执行检查', check: '执行检查', call: '执行能力调用', invoke: '执行能力调用',
        start: '准备实例', stop: '释放实例', release: '释放实例', prepare: '准备能力', wait: '等待资源', run: '执行工作'};
      const events = {before_model: '模型调用前', after_model: '模型返回后', before_tool: '工具调用前', after_tool: '工具返回后',
        before_delivery: '结果交付前', resource: '资源调度'};
      for (const decision of data.decisions || []) {
        const item = node('li'), selected = decision.selected || {};
        const label = kinds[selected.kind] || selected.kind || '调度选择';
        const version = decision.policy_version, step = typeof version === 'object' && version !== null ? version.policy_step_version : version;
        const at = typeof decision.at === 'number' && decision.at > 1e9 ? new Date(decision.at * 1000).toLocaleTimeString('zh-CN') : decision.at == null ? '' : String(decision.at);
        const target = (state?.capabilities || []).find(unit => unit.id === selected.unit_id)?.name ||
          (state?.capability_packages || []).find(pkg => pkg.group_id === selected.group_id)?.name;
        item.append(node('strong', [label, selected.hook || target].filter(Boolean).join(' · ')),
          node('p', [at, events[decision.event] || decision.event,
            Number.isFinite(decision.candidates_count) ? decision.candidates_count + ' 个可选动作' : '',
            step == null ? '' : '策略更新序号 ' + step].filter(Boolean).join(' · '), 'card-meta'));
        timeline.append(item);
      }
      if (timeline.children.length) decisionContent.append(timeline);
      else decisionContent.append(node('p', '本次未记录需要策略决定的操作。必需步骤由平台执行。', 'card-meta'));
      if (data.decisions_truncated) decisionContent.append(node('p', '这里显示最近 300 次调度选择，早先记录仍保留在执行记录中。', 'card-note'));
      decisions.append(decisionContent);
      body.append(node('h3', '能力调用'));
      if (!(data.calls || []).length) body.append(node('p', '尚无调用'));
      for (const call of data.calls || []) body.append(node('p', (call.name || '能力') +
        ' · 执行 ' + ms(call.execute_ms) + ' · 启动等待 ' + ms(call.start_wait_ms) +
        (call.cold_instance === true ? ' · 冷启动' : '') + ' · ' + (call.outcome || '未知')));
      body.append(node('h3', '防护检查'));
      if (!(data.checks || []).length) body.append(node('p', '尚无检查结果'));
      const actions = {allow: '放行', skip: '跳过', block: '阻断', replace: '替换内容'};
      const support = {supported: '参考资料支持', unsupported: '参考资料未支持', unknown: '事实支持未判定'};
      for (const item of data.checks || []) body.append(node('p', (item.at || '') + ' · ' + item.hook + ' · ' +
        (actions[item.action] || item.action || '已执行') + (support[item.fact_support] ? ' · ' + support[item.fact_support] : '') +
        (item.reason ? ' · ' + item.reason : '')));
      if ((data.instances || []).length) body.append(node('h3', '关联实例整个生命周期的占用'),
        node('p', '实例可被多个任务复用。驻留时间和资源积分在实例释放后可用。', 'card-note'));
      for (const inst of data.instances || []) body.append(node('p', (inst.name || '实例') +
        (inst.generation != null ? ' · 第 ' + inst.generation + ' 次启动' : '') +
        ' · 驻留 ' + (Number.isFinite(inst.resident_s) ? inst.resident_s.toFixed(2) + ' 秒' : '未取得') +
        ' · 内存 ' + memory(inst.ram_byte_s) + ' · 显存 ' + memory(inst.vram_byte_s)));
      body.append(decisions);
      const identities = node('div', undefined, 'observer-disclosure-body');
      identities.append(node('p', '任务 · ' + taskId, 'card-meta'));
      if (policy?.id) identities.append(node('p', '策略 · ' + policy.id, 'card-meta'));
      for (const inst of data.instances || []) identities.append(node('p', '实例 · ' + inst.instance_id, 'card-meta'));
      if (data.decisions?.length) detailValue(identities, '调度选择原始记录', data.decisions);
      identifiers.replaceChildren(node('summary', '记录编号与诊断'), identities);
      body.append(identifiers);
    } catch (error) { if (!disposed) { feedback.textContent = '运行记录未能更新：' + error.message; feedback.className = 'message-error'; } }
    finally {
      loading = false; refresh.disabled = false;
      if (!disposed && active) timer = setTimeout(() => { void load(); }, 2000);
    }
  }
  refresh.onclick = load;
  close.onclick = () => dialog.close();
  dialog.onclose = () => { disposed = true; clearTimeout(timer); dialog.remove(); };
  const actions = node('div', undefined, 'form-actions');
  actions.append(refresh, close);
  const heading = node('h2', task ? taskDisplayName(task) + '运行记录' : '任务运行记录');
  heading.id = 'task-activity-heading';
  dialog.setAttribute('aria-labelledby', heading.id);
  dialog.append(heading, actions, feedback, body);
  document.body.append(dialog);
  dialog.showModal();
  void load();
}

function planDetails(task) {
  const plan = task.plan;
  if (!plan) return null;
  const detail = node('details', undefined, 'plan-details');
  detail.append(node('summary', '接口提交的执行方案'));
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

function contextDescription(task) {
  const info = task?.context_status;
  if (!info || info.schema !== 'context-window/1') return null;
  const number = value => finiteNumber(value) ? value.toLocaleString('zh-CN') : '未知';
  const parts = ['窗口 ' + number(info.context_window), '当前输入估算 ' + number(info.estimated_input_tokens),
    '输出预留 ' + number(info.requested_output_tokens), '上次实际输入 ' + number(info.last_actual_input_tokens)];
  if (finiteNumber(info.estimated_ratio) && finiteNumber(info.context_window) && info.multimodal_tokens === 'not_present') {
    parts.push('估算占输入预算 ' + (info.estimated_ratio * 100).toFixed(1) + '%');
  }
  const events = Array.isArray(task.context_events) ? task.context_events.filter(item => item.operation === 'context_compaction') : [];
  parts.push('整理 ' + events.length + ' 次');
  return parts.join(' · ');
}

function contextDetails(task) {
  const description = contextDescription(task);
  if (!description) return null;
  const detail = node('details', undefined, 'context-details');
  detail.append(node('summary', '上下文用量与整理'), node('p', description, 'card-meta'));
  const info = task.context_status;
  detail.append(node('p', '可用输入预算 ' + (finiteNumber(info.available_input_tokens) ? info.available_input_tokens.toLocaleString('zh-CN') : '未知') +
    ' · 窗口来源：' + ({declared: '手动填写', provider_metadata: '服务信息', unknown: '未知'}[info.window_basis] || '未知') +
    '。输入量为估算，实际用量以服务返回为准。' + (info.multimodal_tokens === 'UNKNOWN' ? '含图像时无法估算完整占用。' : ''), 'card-note'));
  const events = Array.isArray(task.context_events) ? task.context_events.filter(item => item.operation === 'context_compaction') : [];
  const usages = events.map(event => event.usage).filter(usage => usage && typeof usage === 'object');
  if (usages.length) {
    const total = names => {
      const values = usages.map(usage => names.map(name => usage[name]).find(finiteNumber)).filter(finiteNumber);
      return values.length ? values.reduce((sum, value) => sum + value, 0).toLocaleString('zh-CN') + (values.length < events.length ? '（部分记录）' : '') : '未知';
    };
    detail.append(node('p', '整理用量：输入 ' + total(['prompt_tokens', 'input_tokens']) + ' · 输出 ' + total(['completion_tokens', 'output_tokens']), 'card-meta'));
  }
  return detail;
}

function renderChatContext() {
  $('chat-context-status').textContent = contextDescription(chatContextTask) || '上下文用量：暂无记录';
}

function taskRecordFinished(task) {
  return ['COMPLETED', 'FAILED', 'INTERRUPTED', 'CANCELLED', 'STOPPED', 'TIMEOUT', 'TIMED_OUT', 'CANCELED'].includes(task.status);
}

async function deleteTaskRecords(tasks) {
  if (taskDeleteBusy || !tasks.length) return;
  if (!window.confirm('从任务列表删除这 ' + tasks.length + ' 条已结束记录？\n\n执行依据、会话和研究结果保留。正在执行或仍有调用未收尾的任务不能删除。')) return;
  taskDeleteBusy = true;
  renderTasks();
  try {
    const result = await api('/api/tasks/delete', {task_ids: tasks.map(task => task.id)});
    const deleted = new Set(result.deleted || []);
    state.tasks = (state.tasks || []).filter(task => !deleted.has(task.id));
    olderTasks = olderTasks.filter(task => !deleted.has(task.id));
    if (deleted.has(inspectedTask?.id)) inspectedTask = {id: inspectedTask.id, error: '此任务记录已移除。'};
    showNotice('已从任务列表删除 ' + deleted.size + ' 条记录，执行依据保留。');
  } catch (error) { showNotice(error.message); }
  finally { taskDeleteBusy = false; renderTasks(); await refresh(); }
}

function retainDisplacedTasks(previous, current) {
  // Once older pages are loaded, a task that newer ones push off the polled page stays listed.
  if (olderTasksCursor === undefined) return;
  const kept = new Set([...(current || []), ...olderTasks].map(task => task.id));
  olderTasks = [...(previous || []).filter(task => !kept.has(task.id)), ...olderTasks];
}

async function loadOlderTasks() {
  if (olderTasksBusy) return;
  const cursor = olderTasksCursor === undefined ? state.tasks_older : olderTasksCursor;
  if (cursor === null || cursor === undefined) return;
  olderTasksBusy = true;
  renderTasks();
  try {
    const result = await api('/api/tasks?before=' + encodeURIComponent(cursor) + '&limit=30');
    const seen = new Set([...(state.tasks || []), ...olderTasks].map(task => task.id));
    for (const task of result.tasks || []) {
      if (seen.has(task.id)) continue;
      seen.add(task.id);
      olderTasks.push(task);
    }
    olderTasksCursor = result.older;
  } catch (error) { showNotice(error.message); }
  finally { olderTasksBusy = false; renderTasks(); await refresh(); }
}

async function loadInspectedTask(identity) {
  if (!identity) { inspectedTask = null; return; }
  if (inspectedTask?.id === identity || (state.tasks || []).some(task => task.id === identity) ||
    olderTasks.some(task => task.id === identity)) return;
  const selected = inspectedTask = {id: identity};
  try { selected.record = await api('/api/tasks/' + encodeURIComponent(identity)); }
  catch (error) { selected.error = error.message; }
}

function renderTasks() {
  const tasks = visibleTasks();
  const root = $('task-list');
  root.replaceChildren();
  const byDate = (a, b) => String(b.started_at || b.created_at || '').localeCompare(String(a.started_at || a.created_at || ''));
  const ordered = [...tasks].sort(inspectPage ? byDate : (a, b) =>
    Number(taskRecordFinished(a)) - Number(taskRecordFinished(b)) || byDate(a, b));
  const shown = inspectPage ? ordered : ordered.slice(0, 4);
  $('task-count').textContent = shown.length;
  const identity = inspectPage && inspectView === 'tasks' ? inspectQuery.get('task') : '';
  if (identity) {
    const link = inspectLink('tasks', selectedSource() ? {harness: selectedSource()} : {});
    link.textContent = '返回任务列表';
    root.append(link);
  }
  const appendOlder = () => {
    const olderCursor = olderTasksCursor === undefined ? state.tasks_older : olderTasksCursor;
    if (olderCursor === null || olderCursor === undefined) return;
    const more = node('button', olderTasksBusy ? '正在加载…' : '加载更早的任务', 'secondary-button');
    more.type = 'button';
    more.disabled = olderTasksBusy;
    more.onclick = () => { void loadOlderTasks(); };
    root.append(more);
  };
  if (!shown.length) {
    const error = identity && inspectedTask?.id === identity ? inspectedTask.error : '';
    empty(root, error || (inspectPage && (identity || selectedSource()) ?
      '当前没有符合筛选条件的任务。' : '尚无任务。发送消息后可在这里查看任务进度。'));
    if (error) {
      const retry = node('button', '重新读取任务', 'secondary-button');
      retry.type = 'button';
      retry.onclick = () => { inspectedTask = null; void refresh(); };
      root.append(retry);
    } else if (inspectPage && !identity) appendOlder();
    return;
  }
  for (const task of shown) {
    const title = taskDisplayName(task);
    const meta = [projectDisplayName(task.project_id), policyDate(task.started_at || task.created_at), latency(task.latency_ms)]
      .filter(Boolean).join(' · ');
    const card = node('article', undefined, 'status-card' + (inspectPage ? '' : ' recent-task'));
    card.dataset.itemId = task.id;
    const head = node('div', undefined, 'card-heading');
    head.append(node('strong', title), pill(task.status));
    card.append(head);
    if (!inspectPage) {
      if (meta) card.append(node('p', meta, 'card-meta'));
      const link = inspectLink('tasks', {task: task.id});
      link.textContent = '打开任务记录';
      card.append(link);
      root.append(card);
      continue;
    }
    const actions = node('div', undefined, 'task-record-actions');
    const activity = node('button', '查看运行记录', 'secondary-button');
    activity.type = 'button';
    activity.onclick = () => showTaskActivity(task.id, task);
    actions.append(activity);
    if (task.policy_version) {
      const policy = task.policy_version;
      if (policy.id) {
        const link = inspectLink('scheduler', {policy: policy.id});
        link.textContent = '策略 · ' + (policy.name || '查看版本');
        actions.append(link);
      } else if (policy.name) actions.append(node('span', '策略 · ' + policy.name, 'card-note'));
    }
    card.append(actions);
    const advanced = node('details');
    advanced.append(node('summary', '执行详情与记录管理'), node('p', '任务编号 · ' + task.id, 'card-meta'));
    if (task.phase) advanced.append(node('p', ({conversation: '对话模型', harness_execution: '能力执行'})[task.phase] || task.phase, 'card-meta'));
    if (typeof task.model_calls === 'number') advanced.append(node('p', '模型调用 ' + task.model_calls + ' 次', 'card-meta'));
    if (task.error) advanced.append(node('p', textValue(task.error), 'card-note message-error'));
    for (const detail of [contextDetails(task), planDetails(task), callDetails(task.calls)]) if (detail) advanced.append(detail);
    const remove = node('button', '删除记录', 'secondary-button danger-button'); remove.type = 'button';
    remove.disabled = taskDeleteBusy || !taskRecordFinished(task);
    if (!taskRecordFinished(task)) remove.title = '任务结束并完成调用收尾后可删除';
    remove.onclick = () => { void deleteTaskRecords([task]); };
    advanced.append(remove);
    card.append(advanced);
    const active = !terminalStatuses.has(task.status);
    root.append(foldCard(card, 'task/' + task.id, title, {status: task.status,
      meta: [meta, task.calls?.length ? task.calls.length + ' 次能力调用' : ''].filter(Boolean).join(' · '),
      error: task.error, open: Boolean(inspectQuery.get('task') === task.id || selectedSource() && (active || task === ordered[0]))}));
  }
  if (inspectPage && !identity) {
    appendOlder();
    const finished = tasks.filter(taskRecordFinished);
    const actions = node('details', undefined, 'observer-disclosure');
    actions.append(node('summary', '批量管理记录'));
    const content = node('div', undefined, 'observer-disclosure-body');
    const clear = node('button', taskDeleteBusy ? '正在清理…' : '清理已结束记录（' + finished.length + '）', 'secondary-button danger-button');
    clear.type = 'button'; clear.disabled = taskDeleteBusy || !finished.length;
    clear.onclick = () => { void deleteTaskRecords(finished); };
    content.append(clear, node('p', '只清理当前筛选下的列表。执行依据、会话和研究结果保留。', 'card-note'));
    actions.append(content);
    root.append(actions);
  }
}

function renderUnits() {
  const root = $('unit-list'), units = (state.capabilities || []).filter(unit =>
    (!selectedSource() || unit.source_id === selectedSource()) &&
    (!inspectQuery.get('unit') || !inspectPage || unit.id === inspectQuery.get('unit')));
  root.replaceChildren(); $('unit-count').textContent = units.length;
  if (!units.length) { empty(root, '尚未安装能力。提交完整仓库后，平台会自动准备。'); return; }
  for (const unit of units) {
    const availability = unit.availability || {}, card = node('article', undefined, 'status-card');
    card.dataset.itemId = unit.id;
    const head = node('div', undefined, 'card-heading'); head.append(node('strong', unit.name || unit.tool_name), pill(availability.status || unit.status));
    card.append(head, node('p', unit.description, 'card-note'));
    if (availability.reason || unit.dispatch?.reason) card.append(node('p', availability.reason || unit.dispatch.reason, 'card-note'));
    card.append(node('p', '来源：' + unit.package + ' · ' + (unit.source_version || '版本未知'), 'card-meta'));
    detailValue(card, '输入接口', unit.parameters);
    detailValue(card, '输出与文件', unit.output);
    detailValue(card, '能力说明与来源依据', unit.capability);
    const link = inspectLink('deployments', {harness: unit.source_id}); link.textContent = '查看资源与实例'; card.append(link);
    root.append(foldCard(card, 'capability/' + unit.id, unit.name || unit.tool_name, {
      status: availability.status || unit.status, meta: unit.description, error: availability.reason}));
  }
}

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
      await api('/api/deployments/' + path, {group_id: group.id, operation_id: operationId(),
        ...(['start', 'add-replica'].includes(path) && imageDraft.scope ? {retention_scope_id: imageDraft.scope} : {}), ...body()});
      showNotice(done);
    } catch (error) { showNotice(error.message); }
    finally { deploymentSubmitting.delete(operation); renderedSections.delete('deployments'); await refresh(); }
  };
  return button;
}

function renderInstance(card, instance, sourceId) {
  const box = node('div', undefined, 'operation-item');
  const head = node('div', undefined, 'card-heading');
  head.append(node('strong', instance.generation != null ? '实例 · 第 ' + instance.generation + ' 次启动' : '运行实例'),
    node('span', instanceStateNames[instance.state] || instance.state, 'status-pill ' + statusClass(
      instance.state === 'READY' ? 'READY' : ['FAILED', 'UNKNOWN'].includes(instance.state) ? 'FAILED' :
      ['STARTING', 'DRAINING'].includes(instance.state) ? 'RUNNING' : 'PENDING')));
  box.append(head, node('p', [instance.origin === 'OPERATOR_ATTACHED' ? '手动启动后登记' : '平台启动',
    instance.accepting ? '接受调用' : '暂停接收调用', (instance.in_flight || 0) + ' 项调用未结束'].join(' · '), 'card-meta'));
  if (instance.reason) box.append(node('p', instance.reason, 'card-note'));
  if (instance.drain_blocked || instance.pending_stop?.blocked) box.append(node('p', '停止受阻：' + textValue(instance.drain_blocked || instance.pending_stop.blocked), 'card-note'));
  if (instance.pending_stop && !instance.released) box.append(node('p', '待释放：尚未确认后端对象已删除。', 'card-note'));
  if (instance.apply?.manual_commands) detailValue(box, '后端不可用 · 手动命令', instance.apply.manual_commands);
  if (instance.stop_result?.manual_commands) detailValue(box, '待释放 · 手动操作与确认命令', instance.stop_result.manual_commands);
  detailValue(box, '实例标识与诊断', {id: instance.id, name: instance.name, run_id: instance.run_id,
    endpoint: instance.endpoint, image: instance.image, image_id: instance.image_id,
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

function gpuAllocationDraft(group) {
  const spec = group.service_spec, allocation = group.gpu_allocation || {};
  const saved = spec ? Object.keys(spec.resource_envelope?.devices || {}) : allocation.devices || [];
  const signature = JSON.stringify([saved, spec?.spec_digest, allocation.source]);
  let draft = gpuAllocationDrafts.get(group.id);
  if (!draft || !draft.dirty && !draft.busy && draft.signature !== signature) {
    draft = {signature, selected: new Set(saved), known: new Map(), extra: '', busy: false, dirty: false, feedback: '',
      memory: Object.fromEntries(Object.entries(spec?.resource_envelope?.devices || {}).map(([id, facts]) => [id, facts.memory_mb])),
      basis: spec?.resource_envelope?.basis || ''};
    gpuAllocationDrafts.set(group.id, draft);
  }
  for (const device of [...(state.gpu_devices || []), ...(allocation.device_facts || [])]) draft.known.set(device.gpu_uuid, device);
  for (const id of draft.selected) if (!draft.known.has(id)) draft.known.set(id, {gpu_uuid: id});
  return draft;
}

async function probeGpuAllocation(group) {
  const draft = gpuAllocationDraft(group);
  if (draft.busy) return;
  const devices = monitorGpuSelectors(draft.extra);
  if (!devices.length) { draft.feedback = '请先填写 GPU UUID 或设备序号。'; renderDeployments(); return; }
  draft.busy = true; draft.feedback = '正在识别指定设备…'; renderDeployments();
  try {
    const result = await api('/api/gpus/probe', {devices});
    for (const device of result.devices) {
      if (device.input !== device.gpu_uuid && draft.selected.delete(device.input) && draft.memory[device.input] != null) {
        draft.memory[device.gpu_uuid] = draft.memory[device.input]; delete draft.memory[device.input];
      }
      draft.known.set(device.gpu_uuid, device); draft.selected.add(device.gpu_uuid);
    }
    draft.extra = ''; draft.dirty = true; draft.feedback = '已识别并勾选，请保存 GPU 分配后启动。';
  } catch (error) { draft.feedback = error.message; }
  finally { draft.busy = false; renderedSections.delete('deployments'); renderDeployments(); }
}

async function saveGpuAllocation(group) {
  const draft = gpuAllocationDraft(group);
  if (draft.busy) return;
  draft.busy = true; draft.feedback = '正在核对并保存 GPU 分配…'; renderDeployments();
  try {
    const devices = [...draft.selected], managed = Boolean(group.service_spec);
    const body = managed ? {service_id: group.managed_service_id || group.service_spec.id, devices,
      memory_mb: Object.fromEntries(devices.map(id => [id, Number(draft.memory[id])])), basis: draft.basis.trim()} : {group_id: group.id, devices};
    if (managed && (devices.some(id => !Number.isFinite(body.memory_mb[id]) || body.memory_mb[id] <= 0) || !body.basis)) {
      throw new Error('请为每张所选设备填写正数显存需求上界，并说明依据。');
    }
    await api(managed ? '/api/managed-services/gpus' : '/api/deployments/gpus', body);
    gpuAllocationDrafts.delete(group.id);
    showNotice('GPU 分配已保存，新启动的实例使用该配置。' + (managed ? '服务规格已更新，请正常重绑引用它的连接。' : ''));
    await refresh();
  } catch (error) { draft.feedback = error.message; }
  finally { draft.busy = false; renderedSections.delete('deployments'); renderDeployments(); }
}

function renderGpuAllocationControls(card, group) {
  const draft = gpuAllocationDraft(group), managed = Boolean(group.service_spec);
  const saved = managed ? Object.keys(group.service_spec.resource_envelope?.devices || {}) : group.gpu_allocation?.devices || [];
  const section = disclosure('gpu-allocation/' + group.id, '任务 GPU 分配', {
    meta: saved.length ? '已选 ' + saved.length + ' 张 GPU' : group.gpu_allocation?.source === 'backend' ? '继承后端配置' : '未分配 GPU'});
  const form = node('form', undefined, 'model-form');
  const held = (group.instances || []).some(instance => instance.released !== true && (managed || !['STOPPED', 'FAILED'].includes(instance.state)));
  const disabled = draft.busy || Boolean(group.locked_by_run) || held || group.gpu_allocation?.editable === false || state.deployment_backend?.kind !== 'docker';
  form.append(node('p', '保存后启动的实例使用所选设备，同组任务随实例执行。已有实例须先排空并确认释放后再换卡；这里设置 GPU 可见范围，不独占整卡。', 'card-note'));
  const label = node('label', '添加 GPU UUID 或设备序号'), input = node('input'); input.type = 'text'; input.value = draft.extra;
  input.placeholder = 'GPU-完整 UUID 或 0, 1'; input.dataset.deploymentField = 'gpu-probe/' + group.id; input.disabled = disabled;
  input.oninput = () => { draft.extra = input.value; draft.dirty = true; }; label.append(input);
  const probe = node('button', draft.busy ? '处理中…' : '探查设备', 'secondary-button'); probe.type = 'button'; probe.disabled = disabled;
  probe.onclick = () => { void probeGpuAllocation(group); }; form.append(label, probe);
  const choices = node('fieldset', undefined, 'monitor-fieldset'); choices.append(node('legend', '选择执行设备'));
  if (!draft.known.size) choices.append(node('p', '填写设备标识后探查；资源观察中已识别的设备也可在这里选择。', 'card-note'));
  for (const [id, device] of draft.known) {
    const row = node('div', undefined, 'operation-item'), option = node('label', undefined, 'checkbox-label'), checked = node('input');
    checked.type = 'checkbox'; checked.value = id; checked.checked = draft.selected.has(id); checked.disabled = disabled;
    checked.onchange = () => { checked.checked ? draft.selected.add(id) : draft.selected.delete(id); draft.dirty = true; };
    option.append(checked, node('span', [device.name, device.index != null ? '设备 ' + device.index : '', id].filter(Boolean).join(' · '))); row.append(option);
    if (managed) {
      const memoryLabel = node('label', '此设备的显存需求上界（MiB）'), memory = node('input'); memory.type = 'number'; memory.min = '0.01'; memory.step = 'any';
      memory.value = draft.memory[id] ?? ''; memory.disabled = disabled; memory.placeholder = '填写模型加载及并发所需上界'; memory.dataset.deploymentField = 'gpu-memory/' + group.id + '/' + id;
      memory.oninput = () => { draft.memory[id] = memory.value; draft.dirty = true; }; memoryLabel.append(memory); row.append(memoryLabel);
    }
    choices.append(row);
  }
  form.append(choices);
  if (managed) {
    const basisLabel = node('label', '资源需求依据'), basis = node('input'); basis.type = 'text'; basis.value = draft.basis; basis.disabled = disabled;
    basis.dataset.deploymentField = 'gpu-basis/' + group.id; basis.oninput = () => { draft.basis = basis.value; draft.dirty = true; };
    basisLabel.append(basis); form.append(basisLabel, node('p', '这里填写服务所需显存，探查到的整卡总显存不会自动填入需求。保存后按现有流程重新绑定服务连接。', 'card-note'));
  } else form.append(node('p', '研究运行的 GPU 容量与显存需求仍在研究配置中声明，并与所选 UUID 核对。取消全部勾选并保存表示不提供 GPU。', 'card-note'));
  const save = node('button', '保存 GPU 分配', 'secondary-button'); save.type = 'submit'; save.disabled = disabled;
  form.onsubmit = event => { event.preventDefault(); void saveGpuAllocation(group); }; form.append(save);
  if (disabled && !draft.busy) form.append(node('p', group.gpu_allocation?.edit_reason || group.gpu_allocation?.reason || (state.deployment_backend?.kind !== 'docker' ? '此入口用于 Docker 后端；先配置部署后端。' : '实例或研究运行仍在使用该组，请先结束并确认释放。'), 'card-note'));
  if (draft.feedback) form.append(node('p', draft.feedback, 'form-status'));
  section.append(form); card.append(section);
}

function renderServiceGpuAllocations(root) {
  const services = entries(resourceState().managed_services);
  if (!inspectPage || !services.length) return;
  const section = disclosure('managed-gpu-allocations', '受管模型服务 GPU 分配', {meta: services.length + ' 个已登记服务'});
  for (const service of services) {
    const card = node('article', undefined, 'status-card'); card.dataset.itemId = service.id;
    card.append(node('h4', service.name || service.service_spec?.model || '未命名模型服务'));
    renderGpuAllocationControls(card, service); section.append(card);
  }
  root.append(section);
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
  root.append(foldCard(card, 'deployment-backend', '部署后端', {meta: backend.kind === 'none' ? '未配置' : backend.kind,
    error: controller.error}));
}

function runtimeGroupIdentity(group) {
  const source = (state.harnesses || []).find(item => item.id === group.source_id);
  const units = (state.units || []).filter(unit => unit.runtime_group === group.id && unit.source_id === group.source_id);
  const origin = (state.analyses || []).find(job => job.harness_id === group.source_id &&
    job.id === (group.analysis_id || group.installation_id));
  const jobs = origin ? [origin] : (state.analyses || []).filter(job => job.harness_id === group.source_id &&
    (job.source_version || job.submitted?.report?.source_version) === group.source_version);
  const entries = new Map(jobs.flatMap(job => job.submitted?.candidate?.entries || []).map(entry => [entry.id, entry]));
  for (const unit of units) entries.set(unit.entry_id, unit);
  const members = (group.entries || []).map(id => entries.get(id) || {id});
  const names = members.map(entry => entry.symbol === '__main__' ? entry.path || entry.name || entry.id :
    entry.symbol || entry.name || entry.id);
  const purpose = [...new Set(members.map(entry => entry.capability?.goal || entry.description).filter(Boolean))].join('；');
  return {title: (source?.name || source?.address || group.source_id) + ' · ' + names.slice(0, 3).join('、') +
      (names.length > 3 ? ' 等 ' + names.length + ' 个入口' : ''), purpose: compactText(purpose || '按原入口与生命周期共同运行')};
}

function runtimeGroupError(group) {
  const failedStep = [group.artifact?.build, group.artifact?.publish].find(step =>
    ['FAILED', 'BACKEND_UNAVAILABLE'].includes(step?.status));
  return group.environment_gap || (failedStep && (failedStep.reason || '构建或发布失败')) ||
    (group.instances || []).find(item => item.state === 'FAILED')?.reason ||
    ((group.instances || []).some(item => item.state === 'FAILED') ? '存在失败实例，展开查看事件与操作' : null);
}

function runtimeGroupActive(group) {
  return Boolean(group.locked_by_run || deploymentPreparation.get(group.source_id)?.running ||
    (group.instances || []).some(item => ['STARTING', 'READY', 'DRAINING'].includes(item.state)) ||
    ['RUNNING', 'BUILDING', 'PUBLISHING'].includes(group.artifact?.build?.status) ||
    ['RUNNING', 'PUBLISHING'].includes(group.artifact?.publish?.status));
}

function renderImages(root) {
  const snapshot = state.image_retention || {};
  const section = disclosure('experiment-images', '实验镜像与备份', {
    meta: '整轮保留 · 真 worker 照常释放', error: imageError});
  const card = node('article', null, 'status-card'); section.append(card);
  card.append(node('p', '开始前保留本轮全部运行镜像。引用容器不启动、不占显卡；结束本轮前先完成原生、平台及评分任务。镜像缺失会终止本轮，已有结果保留。', 'card-note'));
  const field = (key, label, initial = '') => {
    const wrapper = node('label', label), input = node('input');
    input.value = imageDraft[key] ?? initial;
    input.oninput = () => { imageDraft[key] = input.value; };
    wrapper.append(input); card.append(wrapper); return input;
  };
  const storage = field('storage', 'Docker 存储的已挂载平台内路径', snapshot.settings?.storage_path || '');
  const directory = field('directory', '备份目录（已有可写挂载，需有足够空间）');
  const metadata = field('metadata', '已有备份的元数据文件路径（迁移或重新登记时使用）');
  const comparisons = field('comparisons', '本轮比较单元（名称及子运行）', '[{"id":"pair-1","run_ids":["native","platform"]}]');
  field('scope', '页面启动实例所用的本轮范围');
  const action = (parent, label, work) => {
    const button = node('button', label); button.type = 'button'; button.disabled = imageBusy;
    button.onclick = async () => {
      if (imageBusy) return;
      imageBusy = true; imageError = ''; renderDeployments();
      try { await work(); }
      catch (error) { imageError = error.message; }
      finally { imageBusy = false; renderedSections.delete('deployments'); await refresh(); renderDeployments(); }
    };
    parent.append(button); return button;
  };
  const controls = node('div', null, 'inline-actions'); card.append(controls);
  action(controls, imageBusy ? '操作中…' : '刷新本机镜像', async () => { imageInventory = await api('/api/images'); });
  action(controls, '核对并保存存储位置', () => api('/api/images/configure', {storage_path: storage.value.trim() || null}));
  const selected = () => [...imageSelected];
  action(controls, '准备本轮', async () => {
    const rows = (imageInventory?.images || []).filter(row => imageSelected.has(row.image_id));
    const groupIds = [...new Set(rows.flatMap(row => row.purposes.filter(p => p.startsWith('worker:') || p.startsWith('service:'))
      .map(p => p.startsWith('worker:') ? p.slice(7) : p)))];
    const result = await api('/api/images/prepare', {images: selected(), group_ids: groupIds,
      comparison_units: JSON.parse(comparisons.value), storage_path: storage.value.trim() || null});
    if (result.failure) throw new Error(result.failure.reason);
    imageDraft.scope = result.id;
    showNotice('本轮镜像已保留。正式运行前选择“开始计量”。');
  });
  action(controls, '备份选中镜像', async () => {
    const result = await api('/api/images/backup', {images: selected(), directory: directory.value.trim()});
    showNotice('归档及摘要已校验：' + result.path + '；实际恢复核验另记。');
  });
  action(controls, '登记已有备份', () => api('/api/images/import-backup', {metadata_path: metadata.value.trim()}));
  if (!imageInventory) card.append(node('p', '点击刷新后显示实时存在性。历史构建成功不能证明镜像现在仍在本机。', 'card-note'));
  for (const row of imageInventory?.images || []) {
    const item = node('div', null, 'status-card'), label = node('label'), check = node('input');
    check.type = 'checkbox'; check.checked = imageSelected.has(row.image_id); check.disabled = imageBusy;
    check.onchange = () => check.checked ? imageSelected.add(row.image_id) : imageSelected.delete(row.image_id);
    label.append(check, node('span', row.purposes.join(' / '))); item.append(label,
      node('p', row.image_id), node('p', `${row.local_status} · 检查于 ${row.checked_at}`),
      node('p', '名称：' + (row.facts?.RepoTags || []).join(', ')),
      node('p', '镜像大小（含共享层）：' + (row.facts?.Size ?? '未知') + ' 字节'),
      node('p', '持有范围：' + (row.scopes.join(', ') || '无') + ' · 已登记备份：' + row.backups.length));
    if (row.reason) item.append(node('p', row.reason, 'error'));
    if (!row.scopes.length && row.local_status === 'PRESENT') action(item, '删除此本机镜像', async () => {
      if (!window.confirm('删除本机镜像？\n' + row.image_id + '\n用途：' + row.purposes.join(', ') + '\n名称：' + (row.facts?.RepoTags || []).join(', ') + '\n确认没有其他使用者需要此镜像。备份文件保留。')) return;
      await api('/api/images/delete', {image_id: row.image_id, tags: row.facts?.RepoTags || [], confirm_unused: true});
      imageInventory = await api('/api/images');
    });
    card.append(item);
  }
  for (const scope of snapshot.scopes || []) {
    const item = node('div', null, 'status-card');
    item.append(node('strong', '本轮 ' + scope.id), node('p', scope.status + ' · ' + scope.images.length + ' 张镜像'),
      node('p', scope.failure?.reason || scope.cleanup_reason || ''),
      node('p', '在途操作：' + Object.keys(scope.tickets || {}).length));
    for (const unit of scope.comparison_units || []) item.append(node('p', `${unit.id}：${unit.status || '待完整完成'} · ${unit.run_ids.join(', ')}`));
    if (scope.status === 'READY_TO_RUN') action(item, '开始计量', () => api('/api/images/begin', {retention_scope_id: scope.id}));
    if (scope.status !== 'CLOSED') {
      action(item, '用于页面启动', async () => { imageDraft.scope = scope.id; });
      action(item, '核对本轮镜像', () => api('/api/images/verify', {retention_scope_id: scope.id}));
      action(item, '结束本轮／重试收尾', async () => {
        const result = await api('/api/images/close', {retention_scope_id: scope.id});
        if (result.status !== 'CLOSED') showNotice('仍有消费者或待核对事项，引用继续保留：' + result.status);
      });
      if (scope.status === 'INTERRUPTED' || Object.values(scope.tickets || {}).some(t => t.session && t.session !== scope.session)) {
        action(item, '核对重启前的在途操作', async () => {
          if (!window.confirm('确认旧平台提交的启动命令已结束？平台将再次核对真实实例和 Docker 消费者；未解决的外部脚本票据仍会保留。')) return;
          await api('/api/images/reconcile', {retention_scope_id: scope.id, confirm_launches_settled: true});
        });
      }
    }
    card.append(item);
  }
  for (const backup of snapshot.backups || []) {
    if (backup.status === 'DELETED') continue;
    const item = node('div', null, 'status-card');
    item.append(node('strong', '备份 ' + backup.id), node('p', backup.path),
      node('p', `${backup.status} · ${backup.bytes} 字节 · 恢复核验：${backup.restored_at || '尚未执行'}`),
      node('p', '覆盖镜像：' + backup.images.join(', ')),
      node('p', '宿主目录：' + (backup.archive_filesystem?.host_path || '未知')));
    action(item, '准备阶段恢复', async () => {
      const result = await api('/api/images/restore', {backup_id: backup.id, storage_path: storage.value.trim() || null});
      if (result.name_conflicts?.length) throw new Error('镜像已恢复，构建名称冲突保留：' + result.name_conflicts.join(', '));
      imageInventory = await api('/api/images');
    });
    action(item, '删除整个归档', async () => {
      if (!window.confirm('删除此归档及其元数据？\n' + backup.path + '\n覆盖的全部镜像：\n' + backup.images.join('\n') + '\n本机镜像保留。')) return;
      await api('/api/images/delete', {backup_id: backup.id, images: backup.images});
    });
    card.append(item);
  }
  root.append(section);
}

function renderDeployments() {
  const root = $('deployment-list'); root.replaceChildren();
  const groups = (state.runtime_groups || []).filter(group => !selectedSource() || group.source_id === selectedSource());
  const packages = state.capability_packages || [];
  if (!groups.length) {
    empty(root, '仓库准备完成后会在这里显示能力包与实例。');
    const link = inspectLink('analyses', selectedSource() ? {harness: selectedSource()} : {});
    link.textContent = '查看仓库准备进度';
    root.append(link);
  }
  for (const group of groups) {
    const installed = packages.find(item => item.group_id === group.id);
    const source = (state.harnesses || []).find(item => item.id === group.source_id);
    const title = installed?.name || group.name || source?.name || '未命名能力包';
    const availability = installed?.availability || {};
    const error = availability.reason || runtimeGroupError(group);
    const instances = group.instances || [];
    const live = instances.filter(heldInstance);
    const past = instances.filter(instance => !heldInstance(instance));
    const card = node('article', undefined, 'status-card'); card.dataset.itemId = group.id;
    const head = node('div', undefined, 'card-heading');
    head.append(node('strong', title), pill(availability.status || group.status));
    card.append(head);
    if (error) card.append(node('p', error, 'card-note'));
    const members = (state.capabilities || []).filter(unit => unit.runtime_group === group.id);
    card.append(node('p', members.map(unit => unit.name || unit.tool_name).join(' · ') || '等待可用能力', 'card-note'));
    if (installed?.preparation) detailValue(card, '运行环境准备', installed.preparation);
    if (installed?.resources?.length) {
      const resources = node('details'); resources.open = availability.status === 'NEEDS_INPUT';
      resources.append(node('summary', '资源 · ' + installed.resources.filter(item => item.bound).length + ' / ' + installed.resources.length + ' 已匹配'));
      for (const resource of installed.resources)
        resources.append(node('p', resource.description + ' · ' + (resource.bound ? '已绑定' : resource.required ? '需要补充' : '可选'), 'card-note'));
      if (inspectPage) capabilityResourceForm(resources, installed);
      card.append(resources);
    }
    for (const instance of live) renderInstance(card, instance, group.source_id);
    if (!live.length) card.append(node('p', '当前没有运行或待释放的实例。', 'card-note'));
    if (past.length) {
      const history = node('details');
      history.append(node('summary', '近期已结束的实例 · ' + past.length + ' 条'));
      for (const instance of past) renderInstance(history, instance, group.source_id);
      card.append(history);
    }
    if (inspectPage) {
      const advanced = node('details');
      advanced.append(node('summary', '手动运行与诊断'), node('p', '部署编号 · ' + group.id, 'card-meta'));
      advanced.append(node('p', '通常由请求按需准备实例，空闲实例按调度配置释放。操作前会检查在途调用及资源归属。', 'card-note'));
      if (installed) advanced.append(generationAction('重新准备运行环境', '/api/capabilities/prepare', {group_id: group.id}));
      const actions = node('div', undefined, 'form-actions');
      actions.append(deploymentButton(group, 'start', 'start', '启动实例'),
        deploymentButton(group, 'prewarm', 'prewarm', '初始化实例'),
        deploymentButton(group, 'drain_stop', 'drain-stop', '排空后停止'));
      advanced.append(actions);
      renderGpuAllocationControls(advanced, group);
      detailValue(advanced, '能力包与运行契约', group.contract);
      if (group.artifact) detailValue(advanced, '镜像构建记录', group.artifact);
      card.append(advanced);
    }
    if (!inspectPage) card.append(inspectLink('deployments', {harness: group.source_id}));
    root.append(foldCard(card, 'capability-package/' + group.id, title, {
      status: availability.status || group.status,
      meta: [source?.name, members.length + ' 个能力', live.length + ' 个运行或待释放实例'].filter(Boolean).join(' · '),
      error, open: Boolean(selectedSource() || error && live.length)}));
  }
  if (inspectPage) {
    const management = disclosure('deployment-management', '部署管理', {meta: '部署环境、镜像与模型服务 GPU 分配'});
    renderDeploymentBackend(management);
    if (state.image_retention) renderImages(management);
    renderServiceGpuAllocations(management);
    root.append(management);
  }
}

function capabilityResourceForm(root, installed) {
  const form = node('form', undefined, 'resource-form'), readers = [];
  for (const resource of installed.resources) {
    const key = 'binding/' + installed.group_id + '/' + resource.id;
    const label = node('label', resource.description || resource.id);
    let input, read;
    if (['weight', 'service', 'credential'].includes(resource.kind)) {
      input = node('select');
      const option = node('option', resource.bound ? '保留当前绑定' : '选择已登记资源'); option.value = ''; input.append(option);
      const choices = resource.kind === 'weight' ? entries(resourceState().weights) :
        entries(resourceState().connections).filter(item => item.resource_kind !== 'managed');
      for (const item of choices) {
        const value = node('option', item.name || item.model || resourceId(item)); value.value = resourceId(item); input.append(value);
      }
      input.value = deploymentDrafts.get(key) || '';
      read = () => {
        if (!input.value) return undefined;
        const selected = choices.find(item => resourceId(item) === input.value);
        if (resource.kind === 'weight') return {form: 'path', resource_id: input.value, path: '/work/resources/' + input.value};
        const env = 'SEAMPACT_RESOURCE_' + resource.id.toUpperCase();
        if (resource.kind === 'credential') return {form: 'secret', env, connection_id: input.value};
        return {form: 'service', connection_id: input.value, base_url: selected.base_url, model: selected.model,
          parameters: selected.parameters || {}, ...(selected.auth?.mode === 'none' ? {} : {api_key_env: env})};
      };
    } else {
      input = node('textarea'); input.rows = 2;
      input.placeholder = resource.kind === 'device' ? '设备值（JSON，例如 "cpu"）' : '来源要求的参数值（JSON）';
      input.value = deploymentDrafts.get(key) ?? (resource.binding ? JSON.stringify(resource.binding.value) : '');
      read = () => input.value.trim() ? {form: 'value', value: JSON.parse(input.value)} : undefined;
    }
    input.dataset.deploymentField = key;
    input.oninput = () => deploymentDrafts.set(key, input.value);
    label.append(input); form.append(label); readers.push([resource.id, read, key]);
  }
  const actions = node('div', undefined, 'form-actions');
  const save = node('button', '保存并继续准备', 'secondary-button'); save.type = 'submit';
  const feedback = node('span', '', 'form-status'); feedback.setAttribute('role', 'status');
  actions.append(save, feedback); form.append(actions);
  const weights = node('a', '登记权重', 'detail-link'); weights.href = '/configuration?section=weights';
  const connections = node('a', '登记连接', 'detail-link'); connections.href = '/configuration?section=services';
  form.append(weights, connections, node('p', '这里只补充当前能力包需要的已有资源；凭据值在连接配置中单独保存。', 'card-note'));
  form.onsubmit = async event => {
    event.preventDefault(); save.disabled = true;
    try {
      const bindings = {};
      for (const [identity, read] of readers) { const binding = read(); if (binding !== undefined) bindings[identity] = binding; }
      if (!Object.keys(bindings).length) throw new Error('先选择或填写需要补充的资源');
      await api('/api/capabilities/bind', {group_id: installed.group_id, bindings});
      for (const [, , key] of readers) deploymentDrafts.delete(key);
      feedback.textContent = '资源已保存，后台继续准备。'; renderedSections.delete('deployments'); await refresh();
    } catch (error) { feedback.textContent = error.message; }
    finally { save.disabled = false; }
  };
  root.append(form);
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
  if (!inspectPage || inspectView !== 'analyses' || state?.analysis_revisions_observation_pending) return;
  const selected = new Set(revisionSelected.values());
  const results = await Promise.allSettled((state.analysis_revisions || []).filter(item => selected.has(item.id)).map(async item => {
    const version = JSON.stringify(item);
    if (revisionVersions.get(item.id) === version || revisionBusy.has(item.id)) return;
    try {
      const result = await api('/api/analysis/revisions/' + encodeURIComponent(item.id));
      const revision = result.revision || result;
      if (revision.observation_pending) {
        state.analysis_revisions_observation_pending = true;
        revisionVersions.delete(item.id);
        return;
      }
      acceptRevision(revision);
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
  if (revisionBusy.has(id) || state?.analysis_revisions_observation_pending) return;
  revisionBusy.add(id); revisionErrors.delete(id);
  renderedSections.delete('analyses');
  await refresh();
  try {
    if (state?.analysis_revisions_observation_pending) return;
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
    create.disabled = revisionBusy.has(job.id) || state?.analysis_revisions_observation_pending || harness.status === 'DETACHED' || harness.status === 'ANALYZING';
    create.onclick = async () => {
      if (state?.analysis_revisions_observation_pending || revisionBusy.has(job.id)) return;
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
  if (id && !revision) workspace.append(node('p', state?.analysis_revisions_observation_pending ?
    '后台正在检查来源，暂显示已保存修订摘要。' : '正在读取修订…', 'card-note'));
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
      for (const button of Object.values(buttons)) button.disabled = locked || state?.analysis_revisions_observation_pending || harness.status === 'DETACHED';
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
    host_dependency_missing: ['上层入口漏了下游所需资源', '需要分析器补齐该入口的服务或权重需求，并核对原参数或子进程传递。'],
    dependency_closure_unresolved: ['下游调用还没有查完整', '需要补查实际调用的同版源码和子进程目标，不能把未知当作不需要资源。'],
    dependency_closure_unsupported: ['部分原调用暂不支持检查', '需要完善平台的调用分析。'],
    dependency_closure_limit: ['调用分析达到当前上限', '需要完善平台的调用分析，保留已取得的结果。'],
    subprocess_environment_overridden: ['子进程会改写这项资源配置', '需要核对原覆盖顺序，保留原行为；不能直接把上层连接当作下游实际使用的连接。'],
    subprocess_child_source_unavailable: ['还缺实际子进程的源码', '需要分析器补读原脚本，再核对服务参数怎样传入。'],
    subprocess_flow_unsupported: ['这段子进程资源传递尚未核对', '请查看原调用和环境传递的具体缺口，不能靠重复选择资源放行。'],
    configuration_incomplete: ['原配置缺少必填内容', '需要分析器补查原配置的必读项，再使用原默认值或你的配置；不要猜测参数。'],
    dependency_destination_mismatch: ['资源接到了错误的位置', '需要分析器修正资源引用和参数映射；重新登记资源不能解决。'],
    execution_context_unsupported: ['原运行目录或导入环境还没核对好', '需要保留原脚本的工作目录和导入方式，再检查接入。'],
    execution_context_unproven: ['原运行目录还缺源码依据', '需要分析器核对原启动命令所在目录；不要填写服务器上的任意路径。'],
    configuration_flow_unsupported: ['配置传递暂不支持检查', '需要完善平台的配置检查。'],
    configuration_source_unavailable: ['配置调用链还缺原文件', '需要分析器补读实际调用的同版源码；不能用另一版本文件或同名第三方包代替。'],
    completion_contract_unproven: ['任务结果的判定方式还没核对好', '需要核对原输入、输出文件和必需内容；进程退出成功不代表结果齐全。'],
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

function groupEntryIssues(issues) {
  const groups = new Map();
  for (const issue of issues) {
    const summary = entryIssueSummary(issue);
    const key = JSON.stringify([issue.issue_code || issue.category || issue.reason, summary.title,
      summary.action, issue.repair_scope, issue.target]);
    if (!groups.has(key)) groups.set(key, {...summary, issues: [], entries: new Set()});
    const group = groups.get(key);
    group.issues.push(issue);
    if (issue.entry_id) group.entries.add(issue.entry_id);
  }
  return [...groups.values()];
}

function entryIssuesOverview(issues, job) {
  if (!issues.length) return null;
  const groups = groupEntryIssues(issues);
  const root = node('section', undefined, 'entry-issues-overview');
  root.append(node('h4', '需处理的 ' + groups.length + ' 类问题（共 ' + issues.length + ' 项）'));
  const links = new Map();
  for (const group of groups) {
    const item = node('div', undefined, 'entry-issue-group');
    const heading = node('p', undefined, 'entry-issue-heading');
    heading.append(node('strong', group.title), node('span', group.issues.length + ' 项' +
      (group.entries.size ? ' · 涉及 ' + group.entries.size + ' 个入口' : ''), 'card-meta'));
    item.append(heading, node('p', group.action, 'card-note'));
    detailValue(item, '受影响位置与原始记录', group.issues);
    root.append(item);
    if (!inspectPage) continue;
    for (const issue of group.issues) {
      const section = {resources: 'harnesses', services: 'services', weights: 'weights', environment: 'harnesses'}[issue.target];
      if (section || issue.target === 'revision') {
        const href = section ? '/configuration?section=' + section : '#analysis-revision-' + job.id;
        links.set(href, section ? {services: '配置服务', weights: '配置权重', harnesses: '配置接入与环境'}[section] : '查看分析与修正');
      }
    }
  }
  if (links.size) {
    const actions = node('div', undefined, 'entry-issue-actions');
    for (const [href, label] of links) {
      const link = node('a', label, 'detail-link'); link.href = href; actions.append(link);
    }
    root.append(actions);
  }
  return root;
}

function entryReadinessDetails(readiness, job, {showIssues = true} = {}) {
  const root = node('div', undefined, 'entry-readiness');
  root.append(node('p', readiness.status === 'STATIC_READY' ? '接入检查已通过，还需要实际运行验证。' :
    '接入检查尚未通过，请先处理下方问题。', 'card-note'));
  root.append(node('p', '本次检查没有运行原功能；已有结果请查看运行记录。', 'card-note'));
  if (readiness.task_role?.status && readiness.task_role.status !== 'NOT_DECLARED') {
    root.append(node('p', '用途判断：' + taskRoleLabel(readiness.task_role) + '（待核对）', 'card-note'));
  }
  if (showIssues) {
    const overview = entryIssuesOverview(Array.isArray(readiness.issues) ? readiness.issues : [], job);
    if (overview) root.append(overview);
  }
  detailValue(root, '查看原始检查记录', {status: readiness.status, reason: readiness.reason,
    initialization: readiness.initialization, execution: readiness.execution, scope: readiness.scope,
    next_step: readiness.next_step, issues: readiness.issues, task_role: readiness.task_role});
  detailValue(root, '入口参数', readiness.parameters);
  detailValue(root, '入口操作与绑定条件', readiness.operations);
  if (readiness.execution_context) detailValue(root, '原运行目录与导入依据', readiness.execution_context);
  if (readiness.dependency_closure) detailValue(root, '下游调用与所需资源', readiness.dependency_closure);
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
  if (revisionBusy.has(job.id) || state?.analysis_revisions_observation_pending) return;
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
  repair.disabled = revisionBusy.has(job.id) || state?.analysis_revisions_observation_pending || ['DETACHED', 'ANALYZING'].includes(harness.status); repair.onclick = () => prepareRepair(harness, job);
  const recheck = node('button', '重新检查并接入可用入口', 'secondary-button'); recheck.type = 'button';
  recheck.disabled = revisionBusy.has(job.id) || state?.analysis_revisions_observation_pending || ['DETACHED', 'ANALYZING'].includes(harness.status);
  recheck.onclick = async () => {
    if (revisionBusy.has(job.id) || state?.analysis_revisions_observation_pending) return;
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
  const readinessByEntry = new Map((Array.isArray(report.entry_readiness) ? report.entry_readiness : [])
    .map(item => [item.entry_id, item]));
  const issues = bindings.entries.flatMap(result => (readinessByEntry.get(result.id)?.issues || [])
    .map(issue => ({...issue, entry_id: result.id})));
  const overview = entryIssuesOverview(issues, job);
  if (overview) root.append(overview);
  const reasons = {'required source resource mapping is unresolved': '必需资源与原参数的映射未解决',
    'no bindable native entry was discovered': '尚未找到可绑定的原生入口'};
  const table = node('table', undefined, 'research-metrics analysis-entry-table');
  const header = node('tr');
  for (const title of ['入口与用途（分析器描述）', '静态绑定', '当前条件']) header.append(node('th', title));
  const thead = node('thead'); thead.append(header); table.append(thead);
  const body = node('tbody');
  for (const result of bindings.entries) {
    const entry = entries.get(result.id) || {};
    const cell = node('td');
    const title = entry.capability?.goal || entry.description || result.id;
    cell.append(node('p', String(title).split(/[。\n]/)[0], 'analysis-entry-purpose'), node('code', (result.path || entry.path || '') +
      ((result.symbol || entry.symbol) ? ' · ' + (result.symbol || entry.symbol) : '')));
    const row = node('tr');
    const readiness = readinessByEntry.get(result.id);
    const entryIssues = readiness?.issues || [];
    let condition = result.status === 'READY' ? '待实际运行验证' : '查看检查明细';
    if (entryIssues.length) {
      const scopes = new Set(entryIssues.map(issue => issue.repair_scope));
      condition = [...(scopes.has('rule') ? ['需要平台支持'] : []),
        ...(scopes.has('candidate') || scopes.has('evidence') ? ['需要补齐分析结果'] : []),
        ...(scopes.has('resource') ? ['需要选择资源'] : []),
        ...(scopes.has('environment') ? ['需要准备运行环境'] : [])].join('；') || '仍有待处理问题';
    } else if (!readiness && result.reason && result.status !== 'READY') condition = reasons[result.reason] || '查看检查明细';
    const conditions = node('td'); conditions.append(node('p', condition));
    if (entryIssues.length) conditions.append(node('p', groupEntryIssues(entryIssues).length + ' 类问题 · ' +
      entryIssues.length + ' 项，见上方汇总', 'card-meta'));
    row.append(cell, node('td', {READY: '静态可绑定', MISSING: '缺少条件'}[result.status] || statusLabel(result.status), 'entry-binding-status'), conditions);
    body.append(row);
    const detailRow = node('tr', undefined, 'analysis-entry-detail-row');
    const detailCell = node('td'); detailCell.colSpan = 3;
    const detail = node('details', undefined, 'analysis-entry-detail');
    detail.append(node('summary', '检查明细 · ' + (result.path || entry.path || result.id)));
    detailValue(detail, '入口用途（分析器描述）', title);
    if (readiness) detail.append(entryReadinessDetails(readiness, job, {showIssues: false}));
    else detailValue(detail, '原始绑定检查记录', result);
    detailCell.append(detail); detailRow.append(detailCell); body.append(detailRow);
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
  const root = $('analysis-list');
  const jobs = (state.generations || []).filter(job => !selectedSource() || job.source_id === selectedSource())
    .sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)));
  root.replaceChildren(); $('analysis-count').textContent = jobs.length;
  if (!jobs.length) { empty(root, '提交完整仓库后自动生成和准备能力。'); return; }
  for (const job of jobs) {
    const source = (state.harnesses || []).find(item => item.id === job.source_id);
    const card = node('article', undefined, 'status-card'); card.dataset.itemId = job.id;
    const head = node('div', undefined, 'card-heading');
    head.append(node('strong', source?.name || source?.address || job.source_id), pill(job.status));
    card.append(head, node('p', generationPhase(job.phase), 'card-note'));
    card.append(node('p', job.started_at || '', 'card-meta'));
    if (job.error) card.append(node('p', job.error, 'message-error card-note'));
    if (job.gaps?.length) {
      const list = node('ul');
      for (const gap of job.gaps) list.append(node('li', typeof gap === 'string' ? gap : gap.reason || textValue(gap)));
      card.append(list);
    }
    if (job.first_call) detailValue(card, '首次实际调用', job.first_call);
    if (job.coverage_notes?.length) detailValue(card, '本次能力范围', job.coverage_notes);
    if (job.group_id) {
      const link = inspectLink('units', {harness: job.source_id}); link.textContent = '查看能力包与补充资源'; card.append(link);
    }
    const actions = node('div', undefined, 'form-actions');
    if (job.status === 'RUNNING') actions.append(generationAction('取消准备', '/api/generations/cancel', {id: job.id}));
    else if (job.status !== 'AVAILABLE') actions.append(generationAction('重新准备', '/api/generations', {source_id: job.source_id}));
    const detail = node('button', '读取完整记录', 'secondary-button'); detail.type = 'button';
    detail.onclick = async () => {
      detail.disabled = true;
      try { detailValue(card, '本次生成与验证记录', await api('/api/generations/' + encodeURIComponent(job.id)), true); }
      catch (error) { showNotice(error.message); }
      finally { detail.disabled = false; }
    };
    actions.append(detail); card.append(actions); root.append(card);
  }
}

function generationPhase(phase) {
  return ({freezing: '固定仓库版本', generating: '读取源码并生成能力', validating: '检查能力包', matching: '匹配已有资源', binding: '匹配已有资源',
    installing: '安装能力包', preparing: '准备运行环境', first_call: '执行首次实际调用',
    verifying: '验证真实调用', releasing: '等待实例释放', ready: '能力已可用', completed: '本次准备已完成',
    repairing: '修复生成内容', deferred: '等待补充条件', needs_input: '等待补充条件',
    failed: '本次准备失败', cancelled: '准备已取消', interrupted: '准备已中断'})[phase] || phase || '等待准备';
}

function generationAction(label, path, body) {
  const button = node('button', label, 'secondary-button'); button.type = 'button';
  button.onclick = async () => {
    button.disabled = true;
    try { await api(path, body); await refresh(); }
    catch (error) { showNotice(error.message); }
    finally { button.disabled = false; }
  };
  return button;
}

function researchState() { return state?.scheduler?.research || {}; }
function runIdentity(run) { return run.run_id || run.id; }
function researchRunTitle(run) {
  const algorithm = run?.algorithm || run?.manifest?.algorithm;
  const mode = run?.mode || run?.config?.mode;
  return run?.name || run?.label || run?.config?.name || run?.config?.label ||
    [learnerNames[algorithm] || algorithm, researchModeNames[mode]].filter(Boolean).join(' · ') || '研究运行';
}
function finiteNumber(value) { return typeof value === 'number' && Number.isFinite(value); }
function displayNumber(value) { return finiteNumber(value) ? value.toLocaleString('zh-CN', {maximumFractionDigits: 4}) : 'UNKNOWN'; }


function downloadJson(value, filename) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: 'application/json;charset=utf-8'}));
  const link = node('a'); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function exportResearchRun(identity, button) {
  button.disabled = true;
  try {
    const run = await api('/api/scheduler/runs/' + encodeURIComponent(identity));
    const value = {schema: 'seampact-research-progress/1', exported_at: new Date().toISOString(),
      run_id: identity, status: run.status, partial: run.status !== 'COMPLETED',
      resumable_artifact: false, visibility: 'private',
      scope: 'saved task/window results, evidence, configuration and curves; may include local paths; excludes in-flight memory and policy weight files', run};
    downloadJson(value, identity + (value.partial ? '-progress' : '-results') + '.json');
    showNotice('已导出最新已保存结果，包含任务证据与本机路径。进行中的任务在收尾后进入结果；JSON 不包含策略权重文件。');
  } catch (error) { showNotice(error.message); }
  finally { button.disabled = false; }
}


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


function renderResearch() {
  const research = researchState(), root = $('research-run-list'), ready = $('research-readiness');
  ready.replaceChildren(node('p', '共 ' + (research.runs || []).length + ' 条记录，可按原配置查看和导出。', 'card-note'));
  const filter = $('research-filter').value.trim().toLowerCase(), mode = $('research-mode-filter').value;
  const runs = (research.runs || []).filter(run => (!mode || (run.mode || run.config?.mode) === mode) &&
    (!filter || JSON.stringify(run).toLowerCase().includes(filter)));
  root.replaceChildren();
  if (!runs.length) empty(root, '暂无匹配的历史研究记录。');
  for (const run of runs) {
    const identity = runIdentity(run), card = node('article', undefined, 'status-card');
    const head = node('div', undefined, 'card-heading'); head.append(node('strong', researchRunTitle(run)), pill(run.status));
    card.append(head);
    if (run.started_at || run.created_at) card.append(node('p', policyDate(run.started_at || run.created_at), 'card-meta'));
    const view = node('button', '查看结果', 'secondary-button'); view.type = 'button';
    view.onclick = () => { researchSelected.add(identity); renderResearchComparison(); void refreshResearchDetails(); };
    const download = node('button', '导出结果', 'secondary-button'); download.type = 'button';
    download.onclick = () => exportResearchRun(identity, download);
    const actions = node('div', undefined, 'task-record-actions');
    actions.append(view, download);
    card.append(actions);
    detailValue(card, '研究运行编号', identity);
    root.append(card);
  }
  renderResearchComparison();
}

async function refreshResearchDetails() {
  const runs = researchState().runs || [];
  await Promise.all([...researchSelected].map(async identity => {
    const summary = runs.find(run => runIdentity(run) === identity);
    const version = JSON.stringify(summary || identity);
    if (researchLoading.has(identity) || researchDetailVersions.get(identity) === version) return;
    researchLoading.add(identity);
    try {
      const cached = researchDetails.get(identity);
      const historyLength = Array.isArray(cached?.history) ? cached.history.length : 0;
      const curvesLength = Array.isArray(cached?.curves) ? cached.curves.length : 0;
      const incremental = Boolean(cached && cached.view === 'summary' && !cached.error &&
        Number.isFinite(cached.history_total) && Number.isFinite(cached.curves_total) &&
        cached.history_total === historyLength && cached.curves_total === curvesLength);
      const historyAfter = incremental ? cached.history_total : 0;
      const curvesAfter = incremental ? cached.curves_total : 0;
      const detailPath = (historyOffset, curvesOffset) => '/api/scheduler/runs/' + encodeURIComponent(identity) +
        '?view=summary&history_after=' + historyOffset + '&curves_after=' + curvesOffset;
      let result = await api(detailPath(historyAfter, curvesAfter));
      const mismatch = result.history_after !== historyAfter || result.curves_after !== curvesAfter ||
        (incremental && (result.history_total < historyLength || result.curves_total < curvesLength));
      if (mismatch || (incremental && researchDetails.get(identity) !== cached)) {
        researchDetails.delete(identity);
        result = await api(detailPath(0, 0));
        researchDetails.set(identity, result);
      } else if (incremental) {
        researchDetails.set(identity, {...result,
          history: [...cached.history, ...(result.history || [])],
          curves: [...cached.curves, ...(result.curves || [])]});
      } else researchDetails.set(identity, result);
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
    ' · 工作集：' + (joint.working_sets?.schema === 'task-working-sets/3'
      ? '按已选调用及保留状态准入' : '查看本次冻结配置') +
    ' · 资源采样：' + (joint.observation ? '随窗口记录' : '未配置'), 'card-meta'));
  const budget = joint.budget_policy;
  if (budget) {
    const points = Array.isArray(budget.points) ? budget.points : [];
    const label = point => point.id + '（优先减少' + (point.objective === 'ram' ? '内存' : '显存') + '）';
    card.append(node('p', '预算条件 PPO · ' + (run.mode === 'freeze'
      ? '冻结点 ' + (budget.freeze_point || '未声明') : '训练点 ' + (points.map(label).join('、') || '查看冻结配置')) +
      ' · 内存尺度 ' + displayNumber(budget.scales?.ram) + ' GiB·秒' +
      ' · 显存尺度 ' + displayNumber(budget.scales?.vram) + ' GiB·秒' +
      ' · 采集块 ' + (budget.blocks?.length ?? '未声明'), 'card-meta'));
    card.append(node('p', '各预算有效选择窗 ' + (run.budget_choice_windows || []).join(' / ') +
      ' · 参数更新 ' + (run.updates ?? '—'), 'card-meta'));
  }
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
      node('td', item.resource_ops_count ?? item.resource_ops?.length ?? 0),
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
      observation:item.resource_observation, budget_policy:item.budget_policy}, null, 2); details.append(evidence); card.append(details);
  }
  card.append(node('p', budget
    ? '预算回报按冻结尺度和当窗乘子组合成本。各窗详情保留原始分项、约束与乘子，实例秒为驻留代理。'
    : '并行度按同时执行的根调用计算，不以实例数量代替；窗口回报＝已归属到学习转移的部分＋未归属部分。', 'card-note'));
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
    const summary = (researchState().runs || []).find(item => runIdentity(item) === identity);
    const card = node('article', undefined, 'status-card'); card.dataset.itemId = identity;
    const head = node('div', undefined, 'card-heading'); head.append(node('strong', researchRunTitle(summary || run)));
    const close = node('button', '移出对照', 'icon-button'); close.type = 'button';
    close.onclick = () => { researchSelected.delete(identity); renderResearch(); };
    head.append(close); card.append(head);
    const timestamp = run?.started_at || summary?.started_at || run?.created_at || summary?.created_at;
    if (timestamp) card.append(node('p', policyDate(timestamp), 'card-meta'));
    if (!run) { empty(card, '正在读取结果…'); grid.append(card); continue; }
    detailValue(card, '研究运行编号', identity);
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
      detailValue(root, '第 1 项与第 ' + (selected.indexOf(identity) + 1) + ' 项的配置差异',
        differences.length ? differences : '保存的配置内容相同。');
    }
  }
}

function resourceState() { return state?.resources || {}; }
function resourceId(record) { return record?.id || record?.connection_id || record?.resource_id; }
const resourceKindNames = {cloud: '云端托管', borrowed: '自行运行或借用', managed: '由平台启动'};
const connectionCapabilityNames = {text: '文本', image: '图像', embedding: '向量嵌入', retrieval: '检索'};

function savedConnections() {
  return entries(resourceState().connections).filter(connection => connection.base_url || connection.model);
}

function connectionName(connection) {
  const name = connection?.name || connection?.model;
  return Object.hasOwn(modelRoleLabels, name) ? modelRoleLabels[name] : name || '未命名连接';
}

function connectionRoles(connection) {
  const id = resourceId(connection);
  return Object.entries(modelRoleLabels).filter(([role]) => state?.models?.[role]?.connection_id === id)
    .map(([, label]) => label);
}

function canEditResource(form) {
  return !form.dataset.submitting && (!form.dataset.dirty || window.confirm('放弃此表单中尚未保存的更改？'));
}

function showResourceEditor(form) {
  form.hidden = false;
  form.scrollIntoView({block: 'start'});
  (form.elements.name || form.elements.credential)?.focus({preventScroll: true});
}

function closeResourceEditor(form) {
  if (!canEditResource(form)) return;
  form.reset();
  delete form.dataset.dirty;
  form.hidden = true;
}

function managedServiceOptions(value) {
  const form = $('connection-form'), select = form.elements.managed_service_id;
  const previous = value === undefined ? select.value : value;
  const services = entries(resourceState().managed_services);
  const options = services.map(service => [service.managed_service_id || service.id,
    service.name || service.service_spec?.model || service.model || '未命名服务']);
  if (previous && !options.some(([id]) => id === previous)) options.push([previous, '原服务规格不可用']);
  const signature = JSON.stringify(options);
  if (select.dataset.options !== signature) {
    select.replaceChildren();
    const prompt = node('option', '选择已登记的服务规格'); prompt.value = ''; select.append(prompt);
    for (const [id, label] of options) {
      const option = node('option', label); option.value = id; select.append(option);
    }
    select.dataset.options = signature;
  }
  if (select.value !== (previous || '')) select.value = previous || '';
}
function connectionResourceChanged() {
  const form = $('connection-form');
  const managed = form.elements.resource_kind.value === 'managed';
  $('connection-managed-service-label').hidden = !managed;
  form.elements.managed_service_id.required = managed;
}
function connectionOptions(select, value, connections = savedConnections()) {
  const signature = JSON.stringify(connections.map(item => [resourceId(item), connectionName(item), item.model]));
  if (select.dataset.options !== signature) {
    const previous = select.value;
    const option = node('option', connections.length ? '选择服务连接' : '暂无可选连接'); option.value = '';
    select.replaceChildren(option);
    for (const connection of connections) {
      const name = connectionName(connection);
      const item = node('option', name + (connection.model && connection.model !== name ? ' · ' + connection.model : ''));
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

function showConnectionCredentials(connection) {
  const form = $('connection-credential-form');
  if (!connection) return;
  const id = resourceId(connection);
  if (!form.hidden && form.elements.connection_id.value === id) { showResourceEditor(form); return; }
  if (!canEditResource(form)) return;
  form.reset();
  delete form.dataset.dirty;
  form.elements.connection_id.value = id;
  $('credential-title').textContent = '凭据 · ' + connectionName(connection);
  $('credential-form-status').textContent = '';
  updateCredentialEditor();
  showResourceEditor(form);
}

function updateCredentialEditor() {
  const form = $('connection-credential-form');
  const connection = savedConnections().find(item => resourceId(item) === form.elements.connection_id.value);
  if (!connection) return;
  $('credential-current').textContent = '当前认证：' + credentialSummary(connection);
  $('connection-clear-credential').hidden = connection.auth?.mode === 'none';
}

function readableResourceStatus(status, kind) {
  const labels = {NOT_RUN: '未执行', NOT_CHECKED: '未检查', UNCHECKED: '未检查',
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

function outputTokenChoice(profile) {
  return profile?.declared_max_output_tokens ?? profile?.parameters?.max_completion_tokens ?? profile?.parameters?.max_tokens ?? null;
}

function applyOutputTokenChoice(body, input, previous) {
  const limit = input.value === '' ? null : Number(input.value);
  const original = outputTokenChoice(previous);
  body.max_output_tokens = limit === original ? (previous?.declared_max_output_tokens ?? null) : limit;
  if (limit === original) return;
  const parameters = {...(body.parameters ?? previous?.parameters)};
  for (const key of ['max_tokens', 'max_completion_tokens']) {
    // Keep a previously configured wire field in sync when only the numeric control changed.
    if (parameters[key] === undefined || parameters[key] !== previous?.parameters?.[key]) continue;
    if (limit === null) delete parameters[key];
    else parameters[key] = limit;
    body.parameters = parameters;
  }
}

function editConnection(connection) {
  const form = $('connection-form');
  if (!form.hidden && form.elements.id.value === resourceId(connection)) { showResourceEditor(form); return; }
  if (!canEditResource(form)) return;
  delete form.dataset.dirty;
  form.elements.id.value = resourceId(connection) || '';
  for (const field of ['name', 'base_url', 'model', 'service_version']) form.elements[field].value = connection[field] || '';
  form.elements.context_window.value = connection.declared_context_window ?? '';
  form.elements.max_output_tokens.value = outputTokenChoice(connection) ?? '';
  form.elements.context_management.checked = connection.context_management !== false;
  form.elements.protocol.value = connection.protocol || 'openai-compatible';
  form.elements.resource_kind.value = connection.resource_kind || 'borrowed';
  managedServiceOptions(connection.managed_service_id); connectionResourceChanged();
  form.elements.parameters.value = textValue(connection.parameters || {});
  form.elements.auth_mode.value = connection.auth?.mode || 'none';
  form.elements.auth_env.value = connection.auth?.env || '';
  form.dataset.savedAuthMode = form.elements.auth_mode.value;
  form.dataset.savedAuthEnv = form.elements.auth_env.value;
  for (const input of form.querySelectorAll('[name="capability"]')) input.checked = declaredCapabilities(connection).includes(input.value);
  form.dataset.savedUrl = connection.base_url || '';
  form.dataset.authAddress = form.dataset.savedUrl;
  $('connection-form-title').textContent = '编辑连接 · ' + connectionName(connection);
  const roles = connectionRoles(connection);
  const usage = $('connection-edit-usage');
  usage.hidden = !roles.length;
  usage.textContent = '修改会应用到随后使用此连接的任务。当前用途：' + roles.join('、');
  $('connection-editor-credentials').hidden = false;
  $('connection-advanced').open = false;
  $('connection-form-status').textContent = '';
  connectionAuthChanged();
  showResourceEditor(form);
}

function connectionAuthChanged() {
  const form = $('connection-form');
  const mode = form.elements.auth_mode.value;
  $('connection-auth-env').hidden = mode !== 'environment';
  form.elements.auth_env.disabled = mode !== 'environment';
  form.elements.auth_env.required = mode === 'environment';
  const note = $('connection-auth-note');
  note.hidden = mode !== 'saved';
  note.textContent = '保存连接后，点击“管理凭据”填写或更换密钥。';
}

function connectionAddressChanged() {
  const form = $('connection-form');
  const address = form.elements.base_url.value.trim();
  if (!form.dataset.savedUrl || address === form.dataset.authAddress) return;
  form.dataset.authAddress = address;
  form.elements.auth_mode.value = 'saved';
  form.elements.auth_env.value = '';
  connectionAuthChanged();
  $('connection-auth-note').textContent = '地址已更改，请核对认证方式，并为新地址设置密钥。';
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
  const query = $('connection-filter').value.trim().toLowerCase();
  const kind = $('connection-kind-filter').value;
  const connections = savedConnections().filter(connection => (!kind || kind === (connection.resource_kind || 'borrowed')) &&
    [connection.name, connectionName(connection), connection.model].some(value => value?.toLowerCase().includes(query)));
  if (!connections.length) empty(root, query || kind ? '没有匹配的服务连接。' : '尚未登记服务连接。');
  const used = connections.filter(connection => connectionRoles(connection).length);
  const other = connections.filter(connection => !connectionRoles(connection).length);
  for (const [label, records] of [['用于平台模型', used], ['其他连接', other]]) {
    if (!records.length) continue;
    const grid = node('div', undefined, 'config-resource-grid');
    grid.append(...records.map(connectionCard));
    root.append(node('h3', label, 'config-group-title'), grid);
  }
  updateConnectionInvocation();
}

function connectionCard(connection) {
  const card = node('article', undefined, 'resource-card'); card.dataset.itemId = resourceId(connection);
  const roles = connectionRoles(connection);
  const kind = connection.resource_kind || 'borrowed';
  const name = connectionName(connection);
  card.append(node('h4', name), node('p', [resourceKindNames[kind],
    connection.protocol === 'http' ? '仓库专用接口' : 'OpenAI 兼容接口',
    connection.model && connection.model !== name ? connection.model : null].filter(Boolean).join(' · '), 'card-meta'));
  if (roles.length) card.append(node('p', roles.join('、'), 'config-usage'));
  const checks = connection.check || connection.checks || {};
  const statuses = node('div', undefined, 'resource-states');
  resourceStatus(statuses, '认证', credentialSummary(connection));
  resourceStatus(statuses, '连通性', readableResourceStatus(checks.status || connection.connection_status), checks.reason || checks.error);
  card.append(statuses);
  const actions = node('div', undefined, 'harness-actions');
  const edit = node('button', '编辑连接', 'secondary-button'); edit.type = 'button'; edit.onclick = () => editConnection(connection);
  const check = node('button', '检查连接', 'secondary-button'); check.type = 'button';
  check.onclick = () => resourceAction(check, '/api/connections/check', {connection_id: resourceId(connection)},
    '连接检查已更新，可在卡片上查看结果。');
  const credential = node('button', '管理凭据', 'secondary-button'); credential.type = 'button';
  credential.onclick = () => showConnectionCredentials(connection);
  actions.append(edit, check, credential);
  const detail = node('details', undefined, 'connection-operation-checks');
  detail.append(node('summary', '技术详情'));
  detailValue(detail, '连接信息', {地址: connection.base_url, 服务版本: connection.service_version,
    配置修订: connection.revision, 配置摘要: connection.config_digest || connection.digest, 最近检查: checks.checked_at});
  const capabilities = declaredCapabilities(connection).map(key => connectionCapabilityNames[key] || key);
  if (capabilities.length) detail.append(node('p', '已声明：' + capabilities.join('、'), 'card-meta'));
  const operations = connectionOperationChecks(connection);
  const capabilityStatus = checks.capability_status || connection.capability_status;
  for (const result of operations) renderConnectionOperation(detail, result);
  if (!operations.length && capabilityStatus) detail.append(node('p', '请求检查：' + readableResourceStatus(capabilityStatus), 'card-meta'));
  if (kind === 'managed') {
    const service = entries(resourceState().managed_services).find(item => (item.managed_service_id || item.id) === connection.managed_service_id);
    detailValue(detail, '平台管理的服务', service ? {名称: service.name || service.service_spec?.model,
      实例状态: (service.instances || []).map(item => item.model_state || item.state)} : '服务规格不可用');
  }
  card.append(actions, detail);
  return card;
}

function editWeight(weight) {
  const form = $('weight-form');
  if (!form.hidden && form.elements.id.value === resourceId(weight)) { showResourceEditor(form); return; }
  if (!canEditResource(form)) return;
  delete form.dataset.dirty;
  form.elements.id.value = resourceId(weight) || '';
  for (const field of ['name', 'path', 'model_revision']) form.elements[field].value = weight[field] || '';
  form.elements.kind.value = weight.kind || 'directory';
  const required = weight.required_files || [];
  form.elements.required_files.value = required.some(item => typeof item === 'object' && Object.keys(item).some(key => key !== 'path')) ?
    textValue(required) : required.map(item => typeof item === 'string' ? item : item.path).join('\n');
  $('weight-form-title').textContent = '编辑模型文件 · ' + (weight.name || '未命名模型文件');
  $('weight-advanced').open = false;
  $('weight-form-status').textContent = '';
  showResourceEditor(form);
}

function renderWeights() {
  const root = $('weight-list'); root.replaceChildren();
  const query = $('weight-filter').value.trim().toLowerCase();
  const weights = entries(resourceState().weights).filter(weight =>
    [weight.name, weight.path].some(value => value?.toLowerCase().includes(query)));
  if (!weights.length) empty(root, query ? '没有匹配的模型文件。' : '尚未登记模型文件。');
  for (const weight of weights) {
    const card = node('article', undefined, 'resource-card'); card.dataset.itemId = resourceId(weight);
    card.append(node('h4', weight.name || '未命名模型文件'), node('p', weight.path, 'resource-path'));
    const check = weight.check || weight.checks || {};
    const statuses = node('div', undefined, 'resource-states');
    const filesStatus = String(check.status).toUpperCase() === 'PRESENT' && check.necessary_files === 'not_declared' ?
      '路径存在 · 必要文件未声明' : readableResourceStatus(check.status || weight.status, 'weight');
    resourceStatus(statuses, '文件检查', filesStatus, check.reason || check.error);
    resourceStatus(statuses, '路径类型', weight.kind === 'file' ? '单个文件' : '目录');
    card.append(statuses);
    const missing = check.missing || check.missing_files || weight.missing_files;
    if (missing?.length) card.append(node('p', '缺失：' + missing.join('、'), 'card-note message-error'));
    if (check.invalid_files?.length) card.append(node('p', '需取得实际文件：' +
      check.invalid_files.map(item => item.path).join('、'), 'card-note message-error'));
    const actions = node('div', undefined, 'harness-actions');
    const edit = node('button', '编辑路径', 'secondary-button'); edit.type = 'button'; edit.onclick = () => editWeight(weight);
    const action = node('button', '检查文件', 'secondary-button'); action.type = 'button';
    action.onclick = () => resourceAction(action, '/api/resources/check', {resource_id: resourceId(weight)},
      '文件检查已更新，可在卡片上查看结果。');
    actions.append(edit, action); card.append(actions);
    detailValue(card, '文件清单与检查记录', {模型版本: weight.model_revision, 必要文件: weight.required_files, 检查: check});
    root.append(card);
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
  const connections = savedConnections();
  const eligible = connections.filter(connection => connection.protocol === 'openai-compatible' && connection.base_url && connection.model);
  for (const role of Object.keys(modelRoleLabels)) {
    const form = $(role + '-model-form');
    const profile = state.models?.[role];
    const selector = form.elements.connection_id;
    const id = profile?.connection_id || '';
    const current = connections.find(connection => resourceId(connection) === id);
    connectionOptions(selector, undefined, eligible);
    if (selector.dataset.current !== id) {
      selector.value = id;
      selector.dataset.current = id;
    }
    selector.disabled = !eligible.length || Boolean(form.dataset.submitting);
    const readiness = $(role + '-model-readiness');
    const name = connectionName(current || profile);
    const status = profile?.credential_error ? '请重新配置密钥' : isReady(profile) ? '已配置' : '请检查地址、模型或认证';
    const text = current || profile?.configured ? name + ' · ' + status : '尚未选择模型';
    if (readiness.textContent !== text) readiness.textContent = text;
    readiness.classList.toggle('message-error', Boolean(profile?.credential_error));
    const link = form.querySelector('.current-model-link');
    link.hidden = !current;
    if (current) link.href = '/configuration?' + new URLSearchParams({section: 'services', connection: id});
    updateModelSelection(form);
  }
}

function updateModelSelection(form) {
  const selector = form.elements.connection_id;
  form.querySelector('[type="submit"]').disabled = Boolean(form.dataset.submitting) || !selector.value || selector.value === selector.dataset.current;
}

function renderHarnesses() {
  const root = $('harness-list'); root.replaceChildren();
  const sources = state.harnesses || [];
  if (!sources.length) { empty(root, '尚未提交完整仓库。'); return; }
  const detached = node('details', undefined, 'config-disclosure');
  const detachedCount = sources.filter(source => source.status === 'DETACHED').length;
  if (detachedCount) detached.append(node('summary', `已移除的仓库（${detachedCount}）`));
  if (detachedCount === sources.length) empty(root, '当前没有接入中的仓库。');
  for (const source of sources) {
    const job = (state.generations || []).filter(item => item.source_id === source.id)
      .sort((a, b) => String(b.started_at).localeCompare(String(a.started_at)))[0];
    const card = node('article', undefined, 'status-card');
    const head = node('div', undefined, 'card-heading');
    head.append(node('strong', source.name || source.address || '未命名仓库'), pill(source.status === 'DETACHED' ? source.status : job?.status || source.status));
    card.append(head);
    if (source.address && source.name && source.name !== source.address) card.append(node('p', source.address, 'card-note'));
    const link = inspectLink('analyses', {harness: source.id}); link.textContent = '查看准备记录'; card.append(link);
    if (source.status !== 'DETACHED' && job?.status !== 'RUNNING' && job?.status !== 'AVAILABLE')
      card.append(generationAction('准备此仓库能力', '/api/generations', {source_id: source.id}));
    (source.status === 'DETACHED' ? detached : root).append(card);
  }
  if (detachedCount) root.append(detached);
}

async function selectRoleConnection(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const status = form.querySelector('.form-status');
  const connectionId = form.elements.connection_id.value;
  if (form.dataset.submitting || !connectionId || connectionId === form.elements.connection_id.dataset.current) return;
  const role = form.dataset.role;
  const current = state.models?.[role];
  const body = {role, connection_id: connectionId};
  if (current?.connection_id && current.config_digest && Number.isInteger(current.revision)) {
    body.expected_current = {connection_id: current.connection_id, config_digest: current.config_digest, revision: current.revision};
  }
  form.dataset.submitting = 'true';
  form.elements.connection_id.disabled = true;
  updateModelSelection(form);
  status.textContent = '正在保存…';
  try {
    await api('/api/models/select', body);
    status.textContent = '已更新后续任务使用的模型。';
    await refresh();
  } catch (error) { status.textContent = error.message; }
  finally {
    delete form.dataset.submitting;
    hydrateModels();
  }
}

async function saveConnection(event) {
  event.preventDefault();
  const form = event.currentTarget, status = $('connection-form-status');
  if (form.dataset.submitting) return;
  connectionAddressChanged();
  const button = form.querySelector('[type="submit"]'); button.disabled = true;
  form.dataset.submitting = 'true';
  try {
    const parameters = JSON.parse(form.elements.parameters.value.trim() || '{}');
    if (!parameters || typeof parameters !== 'object' || Array.isArray(parameters)) throw new Error('非敏感参数需要 JSON 对象');
    const body = {name: form.elements.name.value.trim(), protocol: form.elements.protocol.value,
      base_url: form.elements.base_url.value.trim(), model: form.elements.model.value.trim(),
      service_version: form.elements.service_version.value.trim() || null, parameters,
      context_window: form.elements.context_window.value ? Number(form.elements.context_window.value) : null,
      context_management: form.elements.context_management.checked,
      capabilities: [...form.querySelectorAll('[name="capability"]:checked')].map(input => input.value)};
    body.resource_kind = form.elements.resource_kind.value;
    const previous = entries(resourceState().connections).find(item => resourceId(item) === form.elements.id.value);
    applyOutputTokenChoice(body, form.elements.max_output_tokens, previous);
    if (body.resource_kind === 'managed') body.managed_service_id = form.elements.managed_service_id.value;
    if (form.elements.id.value) body.id = form.elements.id.value;
    const mode = form.elements.auth_mode.value;
    const env = form.elements.auth_env.value.trim();
    if (!previous || form.dataset.savedUrl !== body.base_url || form.dataset.savedAuthMode !== mode || form.dataset.savedAuthEnv !== env) {
      body.auth = {mode};
      if (mode === 'environment') body.auth.env = env;
      if (mode === 'saved' && previous?.base_url === body.base_url && previous.auth?.credential_ref) {
        body.auth.credential_ref = previous.auth.credential_ref;
      }
    }
    const result = await api('/api/connections', body);
    const id = resourceId(result.connection || result);
    if (id) form.elements.id.value = id;
    form.dataset.savedUrl = body.base_url;
    form.dataset.authAddress = body.base_url;
    form.dataset.savedAuthMode = mode;
    form.dataset.savedAuthEnv = env;
    delete form.dataset.dirty;
    const connection = result.connection || result;
    status.textContent = connection.credential_status === 'missing' && mode === 'saved' ?
      '连接已保存。点击“管理凭据”添加密钥。' : '连接已保存。';
    await refresh();
    if (id) {
      $('connection-form-title').textContent = '编辑连接 · ' + body.name;
      $('connection-editor-credentials').hidden = false;
    }
  } catch (error) { status.textContent = error.message; }
  finally { delete form.dataset.submitting; button.disabled = false; }
}

async function saveConnectionCredential(event) {
  event.preventDefault();
  const form = event.currentTarget, status = $('credential-form-status');
  const button = form.querySelector('[type="submit"]');
  if (form.dataset.submitting || !form.elements.connection_id.value) return;
  if (!form.elements.credential.value) { status.textContent = '输入为空，保留已有凭据'; return; }
  button.disabled = true;
  form.elements.credential.disabled = true;
  form.dataset.submitting = 'true';
  try {
    await api('/api/credentials', {connection_id: form.elements.connection_id.value, action: 'save', value: form.elements.credential.value});
    form.elements.credential.value = ''; delete form.dataset.dirty;
    status.textContent = '密钥已保存。'; await refresh();
  } catch (error) { status.textContent = error.message; }
  finally { delete form.dataset.submitting; button.disabled = false; form.elements.credential.disabled = false; }
}

async function clearConnectionCredential(event) {
  const button = event.currentTarget, form = $('connection-credential-form'), status = $('credential-form-status');
  if (form.dataset.submitting || !form.elements.connection_id.value) return;
  if (!window.confirm('移除此连接的认证设置？使用此连接的后续任务将按无认证方式请求服务。')) return;
  button.disabled = true;
  form.dataset.submitting = 'true';
  try {
    await api('/api/credentials', {connection_id: form.elements.connection_id.value, action: 'clear'});
    form.elements.credential.value = ''; delete form.dataset.dirty;
    status.textContent = '认证已移除。'; await refresh();
  } catch (error) { status.textContent = error.message; }
  finally { delete form.dataset.submitting; button.disabled = false; }
}

async function saveWeight(event) {
  event.preventDefault();
  const form = event.currentTarget, status = $('weight-form-status');
  if (form.dataset.submitting) return;
  const button = form.querySelector('[type="submit"]'); button.disabled = true;
  form.dataset.submitting = 'true';
  try {
    const text = form.elements.required_files.value.trim();
    const required = text.startsWith('[') ? JSON.parse(text) : text.split(/\r?\n/).map(value => value.trim()).filter(Boolean);
    if (!Array.isArray(required)) throw new Error('必要文件需要每行一个路径或 JSON 列表');
    const body = {name: form.elements.name.value.trim(), path: form.elements.path.value.trim(), kind: form.elements.kind.value,
      model_revision: form.elements.model_revision.value.trim() || null, execution_environment: 'local', required_files: required};
    if (form.elements.id.value) body.id = form.elements.id.value;
    const result = await api('/api/weights', body), id = resourceId(result.resource || result.weight || result);
    if (id) form.elements.id.value = id;
    delete form.dataset.dirty;
    $('weight-form-title').textContent = '编辑模型文件 · ' + body.name;
    status.textContent = '路径已登记。可以在列表中检查文件。'; await refresh();
  } catch (error) { status.textContent = error.message; }
  finally { delete form.dataset.submitting; button.disabled = false; }
}

async function addHarness(event) {
  event.preventDefault();
  const form = event.currentTarget, status = $('harness-form-status');
  const body = {address: form.elements.address.value.trim()};
  if (form.elements.revision.value.trim()) body.revision = form.elements.revision.value.trim();
  $('add-harness').disabled = true; status.textContent = '正在提交仓库…';
  try {
    const resources = form.elements.resources.value.trim();
    if (resources) {
      body.resources = JSON.parse(resources);
      if (!Array.isArray(body.resources)) throw new Error('已有资源需要 JSON 列表');
    }
    await api('/api/harnesses', body);
    form.reset(); status.textContent = '仓库已提交，后台正在自动准备能力。';
    await refresh();
  } catch (error) { status.textContent = error.message; }
  finally { $('add-harness').disabled = false; }
}

async function refresh() {
  if (refreshing) return;
  refreshing = true;
  try {
    const previousTasks = state?.tasks || [];
    state = await api('/api/state');
    retainDisplacedTasks(previousTasks, state.tasks);
    const focusedTask = inspectPage && inspectView === 'tasks' ? inspectQuery.get('task') : '';
    await loadInspectedTask(focusedTask);
    connected = true;
    $('connection').textContent = '已连接';
    $('version').textContent = state.version ? '核心 ' + state.version : '';
    document.querySelector('.connection-dot').classList.remove('disconnected');
    hydrateModels();
    updateInspectPage();
    renderSection('profile', state.protocol, renderProfile, ['entry-profile-summary']);
    managedServiceOptions();
    renderSection('connections', [resourceState().connections, resourceState().managed_services,
      Object.keys(modelRoleLabels).map(role => state.models?.[role]?.connection_id)], renderConnections, ['connection-list']);
    updateCredentialEditor();
    if (configurationPage && configSection === 'services' && pendingConfigConnection) {
      const connection = savedConnections().find(item => resourceId(item) === pendingConfigConnection);
      pendingConfigConnection = null;
      if (connection) editConnection(connection);
      else showNotice('该连接已不可用。请从列表选择其他连接。');
    }
    renderSection('weights', resourceState().weights, renderWeights, ['weight-list']);
    synchronizeTask();
    renderSection('scheduler', state.scheduler, renderScheduler, ['scheduler-summary']);
    renderSection('research', [state.scheduler?.research, state.units, state.harnesses], renderResearch,
      ['research-readiness', 'research-run-list', 'research-comparison']);
    renderSection('policies', state.scheduler?.policies, () => renderPolicyVersions(state.scheduler?.policies),
      ['policy-current', 'policy-version-list', 'policy-training-list', 'policy-version-detail']);
    if (policySelected && inspectPage && inspectView === 'scheduler') {
      const signature = JSON.stringify([policySelected, (state.tasks || []).map(task => [task.id, task.status])]);
      if (signature !== policyTaskSignature) { policyTaskSignature = signature; void selectPolicyDetail(policySelected, !policyDetails.has(policySelected)); }
    }
    renderSection('tasks', [visibleTasks(), taskDeleteBusy, olderTasks.length, olderTasksCursor === undefined ? state.tasks_older : olderTasksCursor,
      olderTasksBusy, inspectedTask?.error, state.projects], renderTasks, ['task-list']);
    renderSection('units', [state.capabilities, state.harnesses, selectedSource()], renderUnits, ['unit-list']);
    renderSection('deployments', [state.runtime_groups, state.capability_packages, resourceState(), state.gpu_devices,
      state.deployment_backend, state.image_retention, selectedSource()], renderDeployments, ['deployment-list']);
    renderSection('analyses', [state.generations, state.harnesses, selectedSource()], renderAnalyses, ['analysis-list']);
    renderSection('harnesses', [state.harnesses, state.generations], renderHarnesses, ['harness-list']);
    const requestedJob = pendingRevisionId && (state.analysis_revisions || []).find(item => item.id === pendingRevisionId)?.base_analysis_id;
    const target = pendingSectionHash ? $(pendingSectionHash.slice(1)) : requestedJob ? $('analysis-revision-' + requestedJob) :
      pendingInspectItem ? [...document.querySelectorAll('[data-item-id]')].find(item => item.dataset.itemId === pendingInspectItem) : null;
    if (target) { revealSection(target); pendingSectionHash = ''; pendingRevisionId = null; pendingInspectItem = null; }
  } catch (error) {
    connected = false;
    $('connection').textContent = '连接中断';
    document.querySelector('.connection-dot').classList.add('disconnected');
    showNotice(error.message);
  } finally {
    refreshing = false;
    updateComposer();
    void refreshOperationalViews();
  }
}

function operationItemLink(item) {
  if (item.task_id) return inspectLink('tasks', {task: item.task_id});
  if (item.source_id) return inspectLink('analyses', {harness: item.source_id});
  if (item.group_id) return inspectLink('deployments');
  if (item.run_id) return inspectLink('scheduler');
  return null;
}

function renderOperationPreflight() {
  const root = $('operations-status'), banner = $('deployment-update-status');
  root.replaceChildren(); banner.replaceChildren();
  const report = operationsSnapshot;
  const names = {READY: '当前未发现阻塞', BLOCKED: '有工作正在进行', DRAIN: '需要先释放实例或结束记录', UNKNOWN: '仍有状态需要确认'};
  if (operationsError || !report) {
    const text = operationsError ? '检查未完成：' + operationsError + '。当前不能确认操作条件。' : '正在检查活动。';
    root.append(node('p', text, 'card-note message-error'));
    banner.append(node('p', text, 'card-note'));
  } else {
    const card = node('article', undefined, 'status-card');
    card.append(node('strong', names[report.status] || '状态待确认'),
      node('p', '检查时间：' + (report.observed_at || '未记录') + ' · ' + (report.operation === 'update' ? '更新平台' : '调整来源环境'), 'card-meta'));
    if (report.note) card.append(node('p', report.note, 'card-note'));
    for (const item of report.items || []) {
      const line = node('section', undefined, 'operation-item');
      const source = (state?.harnesses || []).find(source => source.id === item.source_id);
      line.append(node('strong', [source?.name, item.title || item.kind].filter(Boolean).join(' · ')),
        node('p', item.reason, 'card-note'), node('p', '下一步：' + item.next_step, 'card-note'));
      const link = operationItemLink(item); if (link) line.append(link);
      if (item.group_id) detailValue(line, '部署编号', item.group_id);
      card.append(line);
    }
    root.append(card);
    banner.append(node('strong', (report.operation === 'update' ? '更新前检查：' : '来源环境检查：') + (names[report.status] || '状态待确认')),
      node('p', (report.items || []).length + ' 项活动或未决状态 · ' + (report.observed_at || ''), 'card-meta'));
  }
  const link = node('a', '查看检查结果与下一步', 'detail-link');
  link.href = '/inspect?view=operations#operations-status';
  banner.append(link);
}

function monitorNumber(value, digits = 2) {
  return typeof value === 'number' && Number.isFinite(value) ? value.toFixed(digits) : '未知';
}

function monitorSeries(samples) {
  const series = new Map();
  let seen = new Set();
  const add = (id, label, unit, value, sample) => {
    if (!series.has(id)) series.set(id, {id, label, unit, points: []});
    seen.add(id);
    series.get(id).points.push({x: sample.monotonic_s, y: typeof value === 'number' && Number.isFinite(value) ? value : null});
  };
  for (const sample of samples || []) {
    seen = new Set();
    for (const row of [...(sample.processes || []), ...(sample.containers || [])]) {
      const id = row.instance_id ? row.scope + ':' + row.instance_id + ':' + row.generation : 'process:' + row.pid;
      const label = row.group_id ? row.group_id + ' · 第' + row.generation + '代' : '平台进程';
      if ('rss_bytes' in row) add(id + '/memory', label + ' · 常驻内存', 'MiB', row.rss_bytes === null ? null : row.rss_bytes / 1048576, sample);
      if ('memory_current_bytes' in row) add(id + '/memory', label + ' · 容器内存（含缓存）', 'MiB', row.memory_current_bytes === null ? null : row.memory_current_bytes / 1048576, sample);
      if ('cpu_seconds' in row) {
        add(id + '/cpu', label + ' · CPU', '核', typeof row.cpu_percent === 'number' ? row.cpu_percent / 100 : null, sample);
      }
    }
    for (const row of sample.devices || []) {
      for (const [key, title, unit] of [['memory_used_mib', '显存', 'MiB'], ['utilization_gpu_percent', '利用率', '%'], ['power_draw_w', '功率', 'W']]) {
        if (key in row) add(row.gpu_uuid + '/' + key, row.gpu_uuid + ' · 整卡' + title, unit, row[key], sample);
      }
    }
    for (const [key, title] of [['calls_active', '执行中调用'], ['calls_waiting', '等待调用'], ['tasks_running', '运行任务']]) {
      if (sample.workload && key in sample.workload) add('workload/' + key, title, '个', sample.workload[key], sample);
    }
    for (const row of series.values()) if (!seen.has(row.id)) row.points.push({x: sample.monotonic_s, y: null});
  }
  return [...series.values()];
}

function monitorFigure(series) {
  const figure = node('figure', undefined, 'research-curve monitor-curve');
  const points = series.points, valid = points.filter(point => point.y !== null);
  figure.append(node('figcaption', series.label + '（' + series.unit + '）'));
  if (!valid.length) { figure.append(node('p', '尚无有效样本。', 'card-note')); return figure; }
  const ns = 'http://www.w3.org/2000/svg', svg = document.createElementNS(ns, 'svg');
  svg.setAttribute('viewBox', '0 0 600 180'); svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', series.label + '时间曲线');
  const minX = points[0].x, maxX = Math.max(minX + 1, points.at(-1).x), maxY = Math.max(1, ...valid.map(p => p.y));
  const px = p => 45 + (p.x - minX) / (maxX - minX) * 535, py = p => 145 - p.y / maxY * 130;
  const axis = document.createElementNS(ns, 'path'); axis.setAttribute('d', 'M45 15 V145 H585'); axis.setAttribute('class', 'curve-axis'); svg.append(axis);
  let segment = [], last = null;
  const draw = () => { if (segment.length) { const path = document.createElementNS(ns, 'polyline'); path.setAttribute('points', segment.join(' ')); path.setAttribute('class', 'curve-line'); svg.append(path); segment = []; } };
  for (const point of points) {
    if (point.y === null) { draw(); last = null; continue; }
    if (last && point.x - last.x > 2.5 * (monitorDetail?.session?.interval_s || 2)) draw();
    segment.push(px(point) + ',' + py(point)); last = point;
  }
  draw();
  for (const point of valid) {
    const dot = document.createElementNS(ns, 'circle'); dot.setAttribute('cx', px(point)); dot.setAttribute('cy', py(point));
    dot.setAttribute('r', '2'); dot.setAttribute('class', 'curve-point'); svg.append(dot);
  }
  for (const [x, y, text] of [[2, 22, monitorNumber(maxY, 1)], [24, 148, '0'], [45, 168, '0秒'], [530, 168, monitorNumber(maxX - minX, 1) + '秒']]) {
    const label = document.createElementNS(ns, 'text'); label.setAttribute('x', x); label.setAttribute('y', y); label.textContent = text; svg.append(label);
  }
  figure.append(svg, node('p', '最近样本窗口；缺失与过长采样间隔不连线。', 'card-note')); return figure;
}

function monitorGpuSelectors(value) {
  return value.trim().split(/[,，\s]+/).filter(Boolean);
}

function renderMonitorGpuDevices(capabilities) {
  const devices = capabilities.gpu_devices || [];
  const input = $('monitor-gpu-devices');
  if (!monitorGpuHydrated && !monitorGpuDirty && !monitorGpuBusy) {
    input.value = devices.map(device => device.gpu_uuid).join(', ');
    monitorGpuHydrated = true;
  }
  input.disabled = monitorGpuBusy;
  $('monitor-gpu-save').disabled = monitorGpuBusy;
  $('monitor-gpu-save').textContent = monitorGpuBusy ? '正在探查…' : '探查并保存';
  const root = $('monitor-gpu-identities'); root.replaceChildren();
  if (!devices.length) root.append(node('p', '尚未手动保存观察设备。部署中已配置的 GPU 也会出现在下方选择列表。', 'card-note'));
  for (const device of devices) {
    root.append(node('p', [device.name || 'GPU', device.index != null ? '设备 ' + device.index : '',
      device.memory_total_mib != null ? device.memory_total_mib + ' MiB' : '', device.gpu_uuid].filter(Boolean).join(' · '), 'card-note'));
  }
}

async function saveMonitorGpus(event) {
  event.preventDefault();
  if (monitorGpuBusy) return;
  const devices = monitorGpuSelectors($('monitor-gpu-devices').value);
  monitorGpuBusy = true; monitorGpuRevision++;
  $('monitor-gpu-feedback').textContent = devices.length ? '正在识别指定 GPU…' : '正在清除手动观察设备…';
  if (monitorSnapshot) renderMonitor();
  try {
    const result = await api('/api/monitor/gpus', {devices});
    // Keep the verified UUIDs even if the following status refresh fails.
    $('monitor-gpu-devices').value = result.devices.map(device => device.gpu_uuid).join(', ');
    monitorGpuDirty = false; monitorGpuHydrated = true;
    if (monitorSnapshot) {
      monitorSnapshot.capabilities.gpu_devices = result.devices;
      monitorSnapshot.capabilities.gpu_uuids = result.gpu_uuids;
    }
    $('monitor-gpu-feedback').textContent = (result.devices.length ? '已识别并保存 ' + result.devices.length + ' 张 GPU。请勾选设备及 GPU 指标。' : '已清除手动设备；部署中配置的 GPU 仍可选择。') +
      (['RUNNING', 'STOPPING'].includes(monitorSnapshot?.current?.status) ? '新配置用于后续记录，当前记录的设备保持固定。' : '');
    monitorSnapshot = await api('/api/monitor');
  } catch (error) { $('monitor-gpu-feedback').textContent = error.message; }
  finally { monitorGpuBusy = false; if (monitorSnapshot) renderMonitor(); }
}

function renderMonitor() {
  if (!monitorSnapshot) return;
  const capabilities = monitorSnapshot.capabilities || {}, current = monitorSnapshot.current;
  renderMonitorGpuDevices(capabilities);
  const running = ['RUNNING', 'STOPPING'].includes(current?.status);
  const metrics = capabilities.metrics || [], gpus = [...new Set([...(capabilities.gpu_uuids || []), ...(running ? current.gpu_uuids || [] : [])])];
  const signature = JSON.stringify([metrics, gpus, capabilities.gpu_devices]);
  if (signature !== monitorMetricsSignature) {
    const chosen = new Set([...$('monitor-metrics').querySelectorAll('input:checked')].map(input => input.value));
    const selectedGpus = new Set([...$('monitor-gpus').querySelectorAll('input:checked')].map(input => input.value));
    $('monitor-metrics').replaceChildren(); $('monitor-gpus').replaceChildren();
    for (const metric of metrics) {
      const label = node('label', undefined, 'monitor-option'), input = node('input');
      input.type = 'checkbox'; input.value = metric.id; input.disabled = metric.available === false || metric.status === 'UNSUPPORTED';
      input.checked = chosen.size ? chosen.has(metric.id) : !metric.id.startsWith('gpu_') && !input.disabled;
      label.append(input, node('span', metric.label || metric.id));
      if (metric.reason) label.append(node('small', metric.reason, 'card-note'));
      $('monitor-metrics').append(label);
    }
    for (const uuid of gpus) {
      const label = node('label', undefined, 'monitor-option'), input = node('input'); input.type = 'checkbox'; input.value = uuid;
      input.checked = running ? (current.gpu_uuids || []).includes(uuid) : selectedGpus.has(uuid);
      const identity = (capabilities.gpu_devices || []).find(device => device.gpu_uuid === uuid);
      label.append(input, node('span', identity?.name ? identity.name + ' · ' + uuid : uuid)); $('monitor-gpus').append(label);
    }
    $('monitor-gpu-fieldset').hidden = !gpus.length; monitorMetricsSignature = signature;
  }
  $('monitor-start').disabled = running || monitorBusy || monitorGpuBusy;
  $('monitor-stop').disabled = !running || current?.status === 'STOPPING' || monitorBusy;
  for (const input of document.querySelectorAll('#monitor-form input')) input.disabled = running || monitorBusy ||
    (input.type === 'checkbox' && metrics.find(metric => metric.id === input.value)?.available === false);
  const active = $('monitor-current'); active.replaceChildren();
  active.append(node('p', current?.status === 'STOPPING' ? '正在等待当前探针结束。记录尚未关闭，请勿启动另一条记录。' : running ? '正在记录：' + (current.label || current.id) + ' · ' + current.interval_s + ' 秒／次。关闭页面不会停止记录。' :
    '当前未采样。已保存的记录和权重不会启动探针或加载模型。', 'card-note'));
  if (running && current.gpu_uuids?.length) active.append(node('p', '本次固定观察 GPU：' + current.gpu_uuids.join(', ') + '。新保存的设备配置用于后续记录。', 'card-note'));
  const records = monitorSnapshot.sessions || [];
  if (!monitorSelection || !records.some(row => row.id === monitorSelection)) monitorSelection = current?.id || monitorSnapshot.latest?.id || records[0]?.id || '';
  const selector = $('monitor-session'); selector.replaceChildren();
  if (!records.length) selector.append(node('option', '尚无记录'));
  for (const record of records) {
    const option = node('option', (record.label || record.id.slice(0, 10)) + ' · ' + statusLabel(record.status) + ' · ' + (record.started_at || ''));
    option.value = record.id; selector.append(option);
  }
  selector.value = monitorSelection;
  renderMonitorRecord();
}

function renderMonitorRecord() {
  renderSection('monitor-record', [monitorDetail, monitorSelection, monitorSeriesChoice], renderMonitorRecordContent, ['monitor-record']);
}

function renderMonitorRecordContent() {
  const root = $('monitor-record'); root.replaceChildren();
  if (!monitorDetail || monitorDetail.session?.id !== monitorSelection) { root.append(node('p', monitorSelection ? '正在读取记录。' : '选择指标后开始一次记录。', 'card-note')); return; }
  const record = monitorDetail.session, samples = monitorDetail.samples || [], summary = monitorDetail.summary || {};
  const card = node('article', undefined, 'status-card');
  const actions = node('div', undefined, 'form-actions');
  for (const [format, label] of [['json', '导出配置与摘要'], ['jsonl', '导出完整样本']]) {
    const link = node('a', label, 'secondary-button'); link.href = '/api/monitor/sessions/' + encodeURIComponent(record.id) + '/export?format=' + format; link.download = record.id + '.' + format; actions.append(link);
  }
  card.append(node('h4', record.label || '资源记录'), node('p', statusLabel(record.status) + ' · 已记录 ' + (summary.sample_count ?? monitorDetail.total ?? record.sample_count ?? samples.length) + ' 次采样', 'card-meta'), actions,
    node('p', '平台进程、容器和整卡分别统计，不能相加。整卡功率与显存不代表单任务开销。导出含本机资源身份，请保留为私有实验材料。', 'card-note'));
  const workload = samples.at(-1)?.workload;
  if (workload?.status === 'OBSERVED') {
    card.append(node('p', '本次记录开始后结束的任务：' + workload.tasks_finished + '；完成 ' + workload.tasks_completed +
      '，失败 ' + workload.tasks_failed + '，中断 ' + workload.tasks_interrupted + '，取消 ' + (workload.tasks_cancelled ?? 0) +
      '，超时 ' + (workload.tasks_timed_out ?? 0) + '。任务状态不代表防护效果通过。', 'card-note'),
      node('p', '上述已结束任务延迟：p50 ' + monitorNumber(workload.latency_p50_ms) + ' ms · p95 ' +
        monitorNumber(workload.latency_p95_ms) + ' ms · 有延迟记录 ' + workload.latency_sample_count + ' 项；未结束任务不在此分布中。', 'card-meta'));
  }
  const series = monitorSeries(samples), table = node('table', undefined, 'monitor-table'), head = node('tr');
  for (const text of ['范围与指标', '最新值', '窗口内采样峰值', '有效样本']) head.append(node('th', text)); table.append(head);
  for (const row of series) {
    const valid = row.points.filter(point => point.y !== null), line = node('tr');
    line.append(node('td', row.label), node('td', monitorNumber(row.points.at(-1)?.y) + ' ' + row.unit),
      node('td', monitorNumber(valid.length ? Math.max(...valid.map(p => p.y)) : null) + ' ' + row.unit), node('td', valid.length + '/' + row.points.length)); table.append(line);
  }
  if (series.length) {
    if (!series.some(row => row.id === monitorSeriesChoice)) monitorSeriesChoice = series[0].id;
    const label = node('label', '时间曲线'), choice = node('select'); choice.id = 'monitor-series';
    for (const row of series) { const option = node('option', row.label); option.value = row.id; choice.append(option); }
    choice.value = monitorSeriesChoice; choice.onchange = () => { monitorSeriesChoice = choice.value; renderMonitorRecord(); };
    label.append(choice); card.append(label, monitorFigure(series.find(row => row.id === monitorSeriesChoice)));
    const detail = disclosure('monitor-values/' + record.id, '各项最新值与有效样本', {open: series.length < 7}); detail.append(table); card.append(detail);
  }
  const latest = samples.at(-1); if (latest) card.append(node('p', '最近采样：' + latest.utc + ' · 采集用时 ' + monitorNumber(latest.sampling_duration_s, 3) + ' 秒', 'card-meta'));
  const totals = node('table', undefined, 'monitor-table'), totalHead = node('tr');
  for (const text of ['范围', '整段记录统计', '有效时间／覆盖']) totalHead.append(node('th', text)); totals.append(totalHead);
  for (const [key, series] of Object.entries(summary.series || {})) {
    if (series.cpu_seconds_delta != null) {
      const row = node('tr'); row.append(node('td', key), node('td', 'CPU ' + monitorNumber(series.cpu_seconds_delta) + ' 秒'), node('td', monitorNumber(series.cpu_valid_time_s) + ' 秒／' + monitorNumber(typeof series.cpu_time_coverage === 'number' ? series.cpu_time_coverage * 100 : null, 1) + '%')); totals.append(row);
    }
    for (const [metric, value] of Object.entries(series.integrals || {})) {
      const memory = ['rss_bytes', 'memory_current_bytes'].includes(metric);
      const label = metric === 'power_draw_w' ? '整卡能量估计' : metric === 'memory_used_mib' ? '整卡显存积分' : memory ? '内存积分' : '整卡利用率积分';
      const unit = metric === 'power_draw_w' ? 'J' : memory || metric === 'memory_used_mib' ? 'MiB·s' : '%·s';
      const row = node('tr'); row.append(node('td', key), node('td', label + ' ' + monitorNumber(value.value_seconds / (memory ? 1048576 : 1)) + ' ' + unit),
        node('td', monitorNumber(value.valid_time_s) + ' 秒／' + monitorNumber(typeof value.valid_time_coverage === 'number' ? value.valid_time_coverage * 100 : null, 1) + '%')); totals.append(row);
    }
  }
  if (totals.children.length > 1) {
    const detail = disclosure('monitor-summary/' + record.id, '整段记录的有效区间统计', {}); detail.append(totals,
      node('p', '仅统计有效相邻读数；缺失区间未外推，CPU 秒与资源时间积分不是同一种量。', 'card-note')); card.append(detail);
  }
  const detail = node('details'); detail.append(node('summary', '完整配置、统计与缺失原因'), node('pre', JSON.stringify({session: record, summary, latest_sample: latest}, null, 2))); card.append(detail);
  root.append(card);
}

async function refreshOperationalViews(force = false) {
  if (operationsLoading || !state || (!force && !(inspectPage && ['operations', 'deployments'].includes(inspectView)))) return;
  operationsLoading = true; const version = operationsRequestVersion, gpuRevision = monitorGpuRevision;
  try {
    const selected = $('operations-source').value;
    const sources = (state.harnesses || []).filter(source => source.status !== 'DETACHED');
    $('operations-source').replaceChildren();
    for (const source of sources) { const option = node('option', source.name || source.id); option.value = source.id; $('operations-source').append(option); }
    if (sources.some(source => source.id === selected)) $('operations-source').value = selected;
    const operation = inspectView === 'deployments' ? 'update' : $('operations-kind').value;
    $('operations-source-label').hidden = operation !== 'source_environment';
    const query = new URLSearchParams({operation});
    if (operation === 'source_environment') query.set('source_id', $('operations-source').value);
    const results = await Promise.allSettled([api('/api/operations/preflight?' + query),
      inspectView === 'operations' ? api('/api/monitor') : Promise.resolve(null)]);
    if (version !== operationsRequestVersion) return;
    if (results[0].status === 'fulfilled') { operationsSnapshot = results[0].value; operationsError = ''; }
    else operationsError = results[0].reason.message;
    renderOperationPreflight();
    if (results[1].status === 'fulfilled' && results[1].value && gpuRevision === monitorGpuRevision && !monitorGpuBusy) {
      monitorSnapshot = results[1].value; renderMonitor();
      if (monitorSelection) {
        const selectedId = monitorSelection;
        const detail = await api('/api/monitor/sessions/' + encodeURIComponent(selectedId) + '?limit=60');
        if (version === operationsRequestVersion && selectedId === monitorSelection) { monitorDetail = detail; renderMonitorRecord(); }
      }
    } else if (results[1].status === 'rejected') $('monitor-feedback').textContent = '资源观察未能更新：' + results[1].reason.message;
  } catch (error) { $('monitor-feedback').textContent = error.message; }
  finally { operationsLoading = false; }
}

async function submitMonitor(stop = false) {
  if (monitorBusy) return;
  monitorBusy = true; $('monitor-feedback').textContent = stop ? '正在结束采样并保存…' : '正在启动采样…'; renderMonitor();
  try {
    let stopping = false;
    if (stop) { const result = await api('/api/monitor/stop', {session_id: monitorSnapshot.current.id}); stopping = result.status === 'STOPPING'; }
    else {
      const body = {label: $('monitor-label').value.trim(), interval_s: Number($('monitor-interval').value),
        metrics: [...$('monitor-metrics').querySelectorAll('input:checked')].map(input => input.value),
        gpu_uuids: [...$('monitor-gpus').querySelectorAll('input:checked')].map(input => input.value)};
      const result = await api('/api/monitor/start', body); monitorSelection = result.session?.id || result.id || '';
    }
    $('monitor-feedback').textContent = stop ? stopping ? '当前探针仍在结束，确认退出后保存记录。' : '记录已保存。' : '采样已开始。'; monitorDetail = null;
  } catch (error) { $('monitor-feedback').textContent = error.message; }
  finally { monitorBusy = false; await refreshOperationalViews(true); renderMonitor(); }
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
$('project-select').onchange = () => {
  if (pendingTask || chatRestoring || projectFileBusy) { renderProjectSelection(); return; }
  activeProjectId = $('project-select').value; projectFilePath = '';
  resetChat();
};
$('create-project').onclick = async () => {
  if (pendingTask || chatRestoring || projectFileBusy) return;
  const name = window.prompt('新项目名称');
  if (name === null || !name.trim()) return;
  projectFileBusy = true; updateComposer();
  try {
    const result = await api('/api/projects', {name: name.trim()});
    const project = result.project || result;
    projects.push(project); activeProjectId = project.id; renderProjectSelection();
    resetChat();
  } catch (error) { showNotice(error.message); }
  finally { projectFileBusy = false; updateComposer(); }
};
$('project-files').ontoggle = () => { if ($('project-files').open) void loadProjectFiles(); };
$('project-application').ontoggle = () => { if ($('project-application').open) { void loadProjectApplication(); void loadPrivacyDemoCases(); } };
$('project-libraries-none').onclick = () => { void saveProjectApplication({enabled_groups: []}); };
$('project-guards-auto').onclick = () => { void saveProjectApplication({bindings: {}}); };
$('project-libraries-refresh').onclick = () => { void loadProjectApplication(); };
$('project-demo-case').onchange = updateComposer;
$('project-demo-start').onclick = () => { void startPrivacyDemo(); };
$('project-file-area').onchange = () => { projectFilePath = ''; $('project-file-feedback').textContent = ''; void loadProjectFiles(); };
$('project-file-up').onclick = () => { projectFilePath = projectFilePath.split('/').slice(0, -1).join('/'); void loadProjectFiles(); };
$('project-file-refresh').onclick = () => { void loadProjectFiles(); };
$('project-file-upload-button').onclick = () => { void uploadProjectFile(); };
$('project-scratch-clear').onclick = async () => {
  if (pendingTask || projectFileBusy) return;
  projectFileBusy = true; updateComposer();
  try {
    await api('/api/projects/cleanup', {project_id: activeProjectId, target: 'scratch'});
    $('project-file-feedback').textContent = '临时文件已清理。';
  } catch (error) { $('project-file-feedback').textContent = error.message; }
  finally { projectFileBusy = false; updateComposer(); }
};
$('project-workspace-clear').onclick = async () => {
  if (!activeChatId || pendingTask || projectFileBusy) return;
  if (!window.confirm('确定清空当前会话的工作区吗？工作区中的本地文件将被删除，无法恢复。项目资料、已交付结果和会话记录会保留。')) return;
  projectFileBusy = true; updateComposer();
  try {
    await api('/api/projects/cleanup', {project_id: activeProjectId, target: 'workspace', conversation_id: activeChatId});
    $('project-file-feedback').textContent = '当前会话工作区已清空。';
    projectFilePath = ''; await loadProjectFiles();
  } catch (error) { $('project-file-feedback').textContent = error.message; }
  finally { projectFileBusy = false; updateComposer(); }
};
$('stop-message').onclick = async () => {
  const pending = pendingTask; if (!pending?.id || pending.cancelling) return;
  pending.cancelling = true; updateComposer();
  try {
    await api('/api/chat/cancel', {task_id: pending.id});
    if (pendingTask === pending) pending.output.meta.textContent = '已请求停止交付 · 远端停算等待实际确认';
  } catch (error) { pending.cancelling = false; showNotice(error.message); }
  updateComposer();
};
$('new-chat').onclick = () => resetChat();
$('toggle-chat-history').onclick = () => {
  $('chat-history').hidden = !$('chat-history').hidden;
  $('toggle-chat-history').setAttribute('aria-expanded', String(!$('chat-history').hidden));
  if (!$('chat-history').hidden) { loadChatHistory(); $('chat-history-search').focus(); }
};
$('chat-history-search').oninput = () => {
  clearTimeout(historySearchTimer); historySearchTimer = setTimeout(loadChatHistory, 180);
};
$('chat-input').oninput = resizeComposer;
$('chat-input').onkeydown = event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    if (!$('send-message').disabled) $('chat-form').requestSubmit();
  }
};
for (const role of Object.keys(modelRoleLabels)) {
  const form = $(role + '-model-form');
  form.onsubmit = selectRoleConnection;
  form.elements.connection_id.onchange = () => {
    form.querySelector('.form-status').textContent = '';
    updateModelSelection(form);
  };
}
{
  const form = $('connection-form');
  const kindLabel = node('label', '服务来源');
  const kind = node('select'); kind.name = 'resource_kind';
  for (const [value, name] of Object.entries(resourceKindNames)) {
    const option = node('option', name); option.value = value; kind.append(option);
  }
  kind.value = 'borrowed'; kindLabel.append(kind);
  const serviceLabel = node('label', '受管服务规格'); serviceLabel.id = 'connection-managed-service-label';
  const service = node('select'); service.name = 'managed_service_id'; serviceLabel.append(service);
  serviceLabel.append(node('small', '普通对话可按需启动此服务。仓库分析需要服务已经可访问。'));
  const advanced = $('connection-advanced');
  advanced.firstElementChild.after(kindLabel, serviceLabel);
  kind.onchange = connectionResourceChanged;
  managedServiceOptions(); connectionResourceChanged();
  const details = node('details', undefined, 'config-disclosure');
  details.append(node('summary', '登记由平台启动的服务（高级）'));
  details.append(node('p', '保存服务规格后，可在连接的高级设置中选择。连接地址与模型须和规格一致。', 'form-note'));
  const editor = node('form', undefined, 'resource-form');
  const label = node('label', '服务规格 JSON');
  const input = node('textarea'); input.rows = 12;
  input.placeholder = '填写 id、model、model_identity、weight_identity、image、argv、endpoint、docker、contract、resource_envelope';
  label.append(input); editor.append(label);
  const actions = node('div', undefined, 'form-actions');
  const save = node('button', '保存服务规格', 'primary-button'); save.type = 'submit';
  const status = node('span', '', 'form-status'); status.setAttribute('role', 'status');
  actions.append(save, status); editor.append(actions); details.append(editor);
  $('connection-diagnostics').after(details);
  editor.onsubmit = async event => {
    event.preventDefault(); save.disabled = true;
    try {
      const spec = JSON.parse(input.value);
      if (!spec || typeof spec !== 'object' || Array.isArray(spec)) throw new Error('服务规格需要 JSON 对象');
      await api('/api/managed-services', spec);
      status.textContent = '服务规格已保存；未启动服务';
      await refresh();
    } catch (error) { status.textContent = error.message; }
    finally { save.disabled = false; }
  };
}
$('connection-form').onsubmit = saveConnection;
$('connection-invoke-form').onsubmit = invokeConnection;
$('connection-invoke-form').elements.connection_id.onchange = () => {
  $('connection-invoke-status').textContent = ''; $('connection-invoke-result').replaceChildren();
  updateConnectionInvocation();
};
$('connection-invoke-form').elements.operation.onchange = updateConnectionInvocation;
$('connection-form').elements.auth_mode.onchange = connectionAuthChanged;
$('connection-form').elements.base_url.oninput = connectionAddressChanged;
$('connection-form').elements.base_url.onchange = connectionAddressChanged;
$('connection-create').onclick = () => {
  const form = $('connection-form');
  if (!form.hidden && !form.elements.id.value) { showResourceEditor(form); return; }
  if (!canEditResource(form)) return;
  form.reset(); form.elements.id.value = '';
  delete form.dataset.dirty;
  delete form.dataset.savedUrl;
  form.elements.resource_kind.value = 'borrowed'; managedServiceOptions(''); connectionResourceChanged();
  $('connection-form-title').textContent = '新增连接'; $('connection-form-status').textContent = '';
  $('connection-edit-usage').hidden = true;
  $('connection-editor-credentials').hidden = true;
  $('connection-advanced').open = false;
  connectionAuthChanged();
  showResourceEditor(form);
};
$('connection-editor-credentials').onclick = () => {
  const id = $('connection-form').elements.id.value;
  showConnectionCredentials(savedConnections().find(item => resourceId(item) === id));
};
$('connection-close').onclick = () => closeResourceEditor($('connection-form'));
$('connection-filter').oninput = renderConnections;
$('connection-kind-filter').onchange = renderConnections;
$('connection-credential-form').onsubmit = saveConnectionCredential;
$('connection-clear-credential').onclick = clearConnectionCredential;
$('credential-close').onclick = () => closeResourceEditor($('connection-credential-form'));
$('weight-form').onsubmit = saveWeight;
$('weight-create').onclick = () => {
  const form = $('weight-form');
  if (!form.hidden && !form.elements.id.value) { showResourceEditor(form); return; }
  if (!canEditResource(form)) return;
  form.reset(); form.elements.id.value = '';
  delete form.dataset.dirty;
  $('weight-form-title').textContent = '登记模型文件'; $('weight-form-status').textContent = '';
  $('weight-advanced').open = false;
  showResourceEditor(form);
};
$('weight-close').onclick = () => closeResourceEditor($('weight-form'));
$('weight-filter').oninput = renderWeights;
for (const id of ['connection-form', 'connection-credential-form', 'weight-form']) {
  $(id).oninput = event => { event.currentTarget.dataset.dirty = 'true'; };
}
$('research-filter').oninput = renderResearch;
$('research-mode-filter').onchange = renderResearch;
$('policy-filter').oninput = () => renderPolicyVersions();
$('policy-status-filter').onchange = () => renderPolicyVersions();
$('policy-refresh').onclick = () => { void refreshPolicies(); if (policySelected) void selectPolicyDetail(policySelected); };
$('policy-save-form').onsubmit = savePolicyVersion;
$('policy-import-form').onsubmit = importPolicyVersion;
$('policy-discover').onclick = () => { void policyAction('/api/policies/discover', {}, '已查找并登记可用的已完成实验。'); };
$('policy-train-form').onsubmit = startPolicyTraining;
$('policy-train-mode').onchange = renderPolicyTrainingForm;
$('policy-train-recipe').onchange = renderPolicyTrainingForm;
$('harness-form').onsubmit = addHarness;
$('operations-refresh').onclick = () => { operationsRequestVersion++; void refreshOperationalViews(true); };
for (const id of ['operations-kind', 'operations-source']) $(id).onchange = () => {
  operationsRequestVersion++; operationsSnapshot = null; operationsError = ''; renderOperationPreflight();
  $('operations-source-label').hidden = $('operations-kind').value !== 'source_environment';
  void refreshOperationalViews(true);
};
$('monitor-form').onsubmit = event => { event.preventDefault(); void submitMonitor(); };
$('monitor-gpu-form').onsubmit = saveMonitorGpus;
$('monitor-gpu-devices').oninput = () => { monitorGpuDirty = true; };
$('monitor-stop').onclick = () => { void submitMonitor(true); };
$('monitor-session').onchange = () => { monitorSelection = $('monitor-session').value; monitorDetail = null; renderMonitorRecord(); void refreshOperationalViews(true); };
$('open-observer').onclick = () => observerOpen(!document.body.classList.contains('observer-open'));
$('close-observer').onclick = () => observerOpen(false);
$('drawer-shade').onclick = () => observerOpen(false);
$('inspect-source').onchange = async event => {
  const query = new URLSearchParams({view: inspectView});
  if (event.target.value) query.set('harness', event.target.value);
  inspectQuery = query;
  pendingSectionHash = ''; pendingRevisionId = null; pendingInspectItem = null;
  if (!event.target.value) {
    for (const key of detailStates.keys()) if (key.startsWith('all/') || key.includes('/all/')) detailStates.delete(key);
  }
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
    if (section) { event.preventDefault(); history.pushState(null, '', target.href); revealSection(section); }
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
void loadProjects();
if (!inspectPage && !configurationPage) restoreChatFromLocation();
refresh();
setInterval(refresh, 1500);
setInterval(updateAnalysisClocks, 1000);
