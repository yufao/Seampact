// Run with: node --test tests/test_ui_disclosures.cjs
const assert = require('node:assert/strict');
const {test} = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/seampact/ui/app.js'), 'utf8');
class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.value = '';
    this.open = false; this.scrollTop = 0; this.isConnected = true; this.events = {};
    this.classList = {add: name => { this.className = [this.className, name].filter(Boolean).join(' '); }};
  }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return (this.text || '') + this.children.map(child => child.textContent).join(''); }
  append(...children) { for (const child of children) { child.parentElement = this; this.children.push(child); } }
  replaceChildren(...children) { this.children = []; this.append(...children); }
  remove() { this.parentElement.children = this.parentElement.children.filter(child => child !== this); }
  setAttribute() {}
  addEventListener(name, handler) { this.events[name] = handler; }
  querySelectorAll(selector) { return descendants(this).slice(1).filter(item => matches(item, selector)); }
  querySelector(selector) {
    if (selector.startsWith(':scope > ')) return this.children.find(item => matches(item, selector.slice(9))) || null;
    return this.querySelectorAll(selector)[0] || null;
  }
  closest(selector) { for (let item = this; item; item = item.parentElement) if (matches(item, selector)) return item; return null; }
  scrollIntoView() { this.scrolled = true; }
}
function matches(item, selector) {
  if (selector === '[data-item-id]') return Boolean(item.dataset.itemId);
  if (selector === '[data-detail-key]') return Boolean(item.dataset.detailKey);
  if (selector.startsWith('.')) return (item.className || '').split(' ').includes(selector.slice(1));
  return item.tagName === selector.toUpperCase();
}
function descendants(root) { return [root, ...root.children.flatMap(descendants)]; }
function load() {
  const roots = Object.fromEntries(['analysis-list', 'analysis-heading', 'analysis-count', 'harness-list', 'deployment-list', 'unit-list', 'unit-count', 'task-list', 'task-count', 'scroller', 'config'].map(id => {
    const element = new Element('div'); element.id = id; return [id, element];
  }));
  const context = vm.createContext({URLSearchParams, location: {hash: ''},
    document: {createElement: tag => new Element(tag), createTextNode: text => { const item = new Element('span'); item.textContent = text; return item; }, getElementById: id => roots[id] || Object.values(roots).flatMap(descendants).find(item => item.id === id),
      querySelector: selector => selector === '.observer-scroll' ? roots.scroller : roots.config},
    inspectPage: true, inspectView: 'analyses', inspectQuery: new URLSearchParams(), state: {}, taskDeleteBusy: false,
    olderTasks: [], olderTasksCursor: undefined, olderTasksBusy: false, inspectedTask: null,
    detailStates: new Map(), renderedSections: new Map(), capabilityDrafts: new Map(),
    deploymentPreparation: new Map(), deploymentDrafts: new Map(), deploymentSubmitting: new Set(), analysisSubmitting: new Set(), sourceUpdating: new Set(), sourceUpdateOptions: new Map(),
    revisionSelected: new Map(), revisionVersions: new Map(), revisionBusy: new Set(), revisionErrors: new Map(),
    acceptRevision: () => {}, showNotice: () => {}, isReady: () => true,
    terminalStatuses: new Set(['COMPLETED', 'FAILED']), statusNames: {FAILED: '失败'},
    empty: (root, text) => root.append(new Element('p')), entryCount: () => null,
    inspectLink: (view, parameters) => { const link = new Element('a'); link.href = '/inspect?' + new URLSearchParams({view, ...parameters}); return link; },
    resourceState: () => ({}), harnessDependencies: () => {}, sourceEnvironment: () => {},
    renderRevisionWorkspace: (card, harness, job) => { const target = new Element('section'); target.id = 'analysis-revision-' + job.id; card.append(target); },
    renderDeploymentBackend: () => {}, renderInstance: () => {}, environmentEvidence: () => {},
    deploymentButton: (group, action, route, label) => { const button = new Element('button'); button.textContent = label; return button; },
    renderGpuAllocationControls: () => {}, renderServiceGpuAllocations: () => {},
    analysisProgress: () => new Element('div'), analysisClassification: () => null,
    analysisResultSummary: () => null, preprocessingSummary: () => null, analysisNextSteps: () => null,
    contextDetails: () => null, callDetails: () => null, planDetails: () => null,
    analysisTranscript: () => new Element('details'), detailValue: () => {},
    analysisExportButton: () => new Element('button'), cancelAnalysisButton: () => null,
    sourceButton: () => new Element('button'), deleteSourceButton: () => new Element('button')});
  const names = ['textValue', 'node', 'selectedSource', 'detailKey', 'disclosureScope', 'compactText', 'disclosure', 'foldCard',
    'revealSection', 'renderSection', 'statusClass', 'statusLabel', 'pill', 'latency', 'visibleUnits', 'visibleTasks',
    'analysisOperationLabel', 'sourceIdentity', 'renderAnalyses', 'renderHarnesses', 'runtimeGroupIdentity', 'runtimeGroupError',
    'runtimeGroupActive', 'heldInstance', 'renderDeployments', 'renderUnits', 'policyDate', 'taskDisplayName',
    'projectDisplayName', 'taskRecordFinished', 'deleteTaskRecords', 'renderTasks', 'refreshRevisionDetails',
    'revisionAction', 'analysisNextSteps', 'empty', 'detailValue', 'generationPhase', 'generationAction'];
  for (const name of names) {
    if (name === 'node') {
      vm.runInContext(source.slice(source.indexOf('const node ='), source.indexOf('\nconst messages =')), context); continue;
    }
    let start = source.indexOf('function ' + name + '('); assert.ok(start >= 0, name);
    if (source.slice(start - 6, start) === 'async ') start -= 6;
    const rest = source.slice(start), end = rest.search(/\n(?:async )?function /);
    vm.runInContext(end < 0 ? rest : rest.slice(0, end), context);
  }
  context.$ = id => roots[id];
  return {context, roots};
}
function state() {
  return {harnesses: [{id: 'a', name: '来源甲', status: 'ANALYZED', analysis_id: 'new'}, {id: 'b', name: '来源乙', status: 'ANALYZED'}],
    generations: [{id: 'done', source_id: 'a', status: 'AVAILABLE', phase: 'ready', started_at: '2026-11-02',
      group_id: 'rg-hash-one', first_call: {status: 'SUCCEEDED'}},
    {id: 'failed', source_id: 'a', status: 'FAILED', phase: 'failed', started_at: '2026-11-01', error: '原始失败原因'}],
    capabilities: [{id: 'u', name: '文档检查', source_id: 'a', runtime_group: 'rg-hash-one', package: '办公文档',
      availability: {status: 'READY'}, parameters: {type: 'object'}}],
    capability_packages: [{group_id: 'rg-hash-one', name: '办公文档'}, {group_id: 'rg-hash-two', name: '办公检索'}],
    analyses: [{id: 'new', harness_id: 'a', source_version: 'v1', status: 'COMPLETED', started_at: '2026-11-02',
      submitted: {candidate: {entries: [{id: 'run', symbol: 'detect', description: '检测图像真伪'}]}}},
      {id: 'old', harness_id: 'a', status: 'FAILED', started_at: '2026-11-01', error: '原始失败原因'}], units: [],
    runtime_groups: [{id: 'rg-hash-one', source_id: 'a', source_version: 'v1', entries: ['run'], status: 'LOCAL_ONLY', instances: []},
      {id: 'rg-hash-two', source_id: 'b', source_version: 'v2', entries: ['other'], status: 'LOCAL_ONLY', instances: []}]};
}
test('task cleanup uses the current source filter and leaves active tasks and execution evidence alone', async () => {
  const {context: c, roots} = load();
  c.inspectView = 'tasks'; c.inspectQuery = new URLSearchParams({harness: 'a'});
  c.state = {units: [], analyses: [], tasks: [
    {id: 'done-a', status: 'COMPLETED', calls: [{unit: {source_id: 'a'}}]},
    {id: 'active-a', status: 'RUNNING', calls: [{unit: {source_id: 'a'}}]},
    {id: 'done-b', status: 'FAILED', calls: [{unit: {source_id: 'b'}}]}]};
  const calls = [], confirmations = [];
  c.window = {confirm: text => { confirmations.push(text); return true; }};
  c.api = async (url, body) => { calls.push({url, body}); return {deleted: body.task_ids}; };
  c.refresh = async () => {};
  c.renderTasks();
  const cleanup = roots['task-list'].querySelectorAll('button').find(button => button.textContent.startsWith('清理已结束'));
  assert.equal(cleanup.textContent, '清理已结束记录（1）');
  assert.equal(roots['task-list'].querySelectorAll('button').filter(button => button.textContent === '删除记录' && button.disabled).length, 1);
  cleanup.onclick();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/tasks/delete');
  assert.deepEqual(Array.from(calls[0].body.task_ids), ['done-a']);
  assert.deepEqual(Array.from(c.state.tasks, task => task.id), ['active-a', 'done-b']);
  assert.match(confirmations[0], /执行依据、会话和研究结果保留/);
});

test('cancelling task cleanup sends nothing and a rejected cleanup keeps records visible', async () => {
  const {context: c} = load();
  const task = {id: 'done', status: 'COMPLETED'}; c.state = {tasks: [task]};
  let calls = 0, notice = '';
  c.window = {confirm: () => false}; c.refresh = async () => {};
  c.api = async () => { calls++; throw new Error('调用仍在收尾'); };
  c.showNotice = message => { notice = message; };
  await c.deleteTaskRecords([task]);
  assert.equal(calls, 0);
  c.window.confirm = () => true;
  await c.deleteTaskRecords([task]);
  assert.equal(calls, 1); assert.equal(c.state.tasks.length, 1);
  assert.equal(c.taskDeleteBusy, false); assert.equal(notice, '调用仍在收尾');
});
test('source list prepares only idle, attached repositories', async () => {
  const {context: c, roots} = load();
  c.state = state();
  c.state.harnesses.push({id: 'archived', name: '已移除来源', status: 'DETACHED'});
  c.state.generations[0].status = 'RUNNING';
  c.renderHarnesses();
  const root = roots['harness-list'];
  const archived = root.querySelector('details');
  assert.equal(archived.open, false);
  assert.match(archived.textContent, /已移除来源/);
  const actions = root.querySelectorAll('button');
  assert.equal(actions.length, 1);
  assert.match(actions[0].parentElement.textContent, /来源乙/);
  c.refresh = async () => {};
  const calls = [];
  c.api = async (url, body) => calls.push([url, body.source_id]);
  await actions[0].onclick();
  assert.deepEqual(calls, [['/api/generations', 'b']]);
});
test('generation shows the failure reason and permits retry only before availability', async () => {
  const {context: c, roots} = load();
  c.state = state();
  c.renderAnalyses();
  const [ready, failed] = roots['analysis-list'].children;
  assert.match(ready.textContent, /能力已可用/);
  assert.doesNotMatch(ready.textContent, /重新准备/);
  assert.match(failed.textContent, /原始失败原因/);
  c.refresh = async () => {};
  const calls = [];
  c.api = async (url, body) => calls.push([url, body.source_id]);
  await failed.querySelectorAll('button').find(item => item.textContent === '重新准备').onclick();
  assert.deepEqual(calls, [['/api/generations', 'a']]);
});
test('detail becoming busy after snapshot does not accept stale data and retries', async () => {
  const {context: c} = load();
  c.state = {analysis_revisions: [{id: 'r'}], analysis_revisions_observation_pending: false};
  c.revisionSelected.set('job', 'r');
  let calls = 0, accepted = 0;
  c.acceptRevision = () => { accepted++; };
  c.api = async () => ({revision: {id: 'r', observation_pending: ++calls === 1}});
  await c.refreshRevisionDetails();
  assert.equal(c.state.analysis_revisions_observation_pending, true);
  assert.equal(accepted, 0);
  assert.equal(c.revisionVersions.has('r'), false);
  c.state.analysis_revisions_observation_pending = false;
  await c.refreshRevisionDetails();
  assert.equal(calls, 2);
  assert.equal(accepted, 1);
});
test('generation evidence stays expanded through refresh and source filtering', () => {
  const {context: c, roots} = load(); c.state = state();
  const render = () => c.renderSection('analyses', c.state, c.renderAnalyses, ['analysis-list']);
  c.state.generations.push({id: 'waiting', source_id: 'b', phase: 'preparing', status: 'RUNNING'});
  render();
  const root = roots['analysis-list'];
  const firstCall = () => root.children.find(item => item.dataset.itemId === 'done').querySelector('details');
  assert.equal(root.children.length, 3);
  assert.equal(firstCall().open, false);
  firstCall().open = true;
  c.state.generations[0].first_call = {status: 'SUCCEEDED', calls: 1};
  c.inspectQuery.set('harness', 'a'); render();
  assert.equal(root.children.length, 2);
  assert.equal(firstCall().open, true);
  assert.doesNotMatch(root.textContent, /来源乙/);
});
test('deployment groups show installed package names and keep internal IDs in diagnostics', () => {
  const {context: c, roots} = load(); c.state = state(); c.renderDeployments();
  const groups = () => roots['deployment-list'].children.filter(item => item.dataset.detailKey?.includes('/capability-package/'));
  assert.equal(groups().length, 2);
  assert.match(groups()[0].children[0].textContent, /办公文档.*来源甲/);
  assert.doesNotMatch(groups()[0].children[0].textContent, /rg-hash-one/);
  const diagnostics = groups()[0].querySelector('details');
  assert.equal(diagnostics.open, false);
  assert.match(diagnostics.textContent, /rg-hash-one/);
  c.inspectQuery.set('harness', 'a'); c.renderDeployments();
  assert.equal(groups().length, 1);
  assert.equal(groups()[0].open, true);
  assert.equal(c.runtimeGroupError({artifact: {build: {status: 'BUILT', reason: '成功说明'}}}), null);
  c.state.analyses.push({id: 'other-revision', harness_id: 'a', source_version: 'v1',
    submitted: {candidate: {entries: [{id: 'run', symbol: 'unrelated_name', description: '其他修订用途'}]}}});
  const identity = c.runtimeGroupIdentity({...c.state.runtime_groups[0], analysis_id: 'new'});
  assert.match(identity.title, /detect/); assert.doesNotMatch(identity.title, /unrelated_name/);
  assert.equal(identity.purpose, '检测图像真伪');
});
test('capability deep links reveal the record and nested interface details', () => {
  const {context: c, roots} = load(); c.state = state();
  c.inspectQuery = new URLSearchParams({unit: 'u'});
  c.renderUnits();
  const record = roots['unit-list'].children[0];
  const detail = record.querySelector('details');
  const schema = detail.querySelector('pre');
  assert.equal(record.open, false);
  c.revealSection(schema);
  assert.equal(record.open, true);
  assert.equal(detail.open, true);
  assert.equal(schema.scrolled, true);
});
test('a selected source with no deployment links to its preparation progress', () => {
  const {context: c, roots} = load(); c.state = state();
  c.state.harnesses.push({id: 'pending', name: '待准备来源', status: 'REGISTERED'});
  c.inspectQuery.set('harness', 'pending'); c.renderDeployments();
  const root = roots['deployment-list'];
  assert.match(root.textContent, /仓库准备完成后/);
  assert.equal(root.querySelector('a').href, '/inspect?view=analyses&harness=pending');
});
test('pending source observation skips detail reads and repeated revision actions, then resumes reads and controls', async () => {
  const {context: c} = load(); c.state = state();
  c.state.analysis_revisions = [{id: 'rev', base_analysis_id: 'new', observation_pending: true}];
  c.revisionSelected.set('new', 'rev');
  let calls = 0; c.api = async () => { calls++; return {revision: {id: 'rev'}}; };
  c.state.analysis_revisions_observation_pending = true;
  await c.refreshRevisionDetails(); await c.revisionAction('rev', 'check');
  assert.equal(calls, 0); assert.equal(c.revisionBusy.size, 0);
  const buttons = descendants(c.analysisNextSteps(c.state.harnesses[0], c.state.analyses[0])).filter(item => item.tagName === 'BUTTON');
  assert.ok(buttons.every(button => button.disabled));
  c.state.analysis_revisions_observation_pending = false;
  await c.refreshRevisionDetails(); assert.equal(calls, 1);
  const available = descendants(c.analysisNextSteps(c.state.harnesses[0], c.state.analyses[0])).filter(item => item.tagName === 'BUTTON');
  assert.ok(available.every(button => !button.disabled));
});
