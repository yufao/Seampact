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
    inspectPage: true, inspectView: 'analyses', inspectQuery: new URLSearchParams(), state: {},
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
    analysisProgress: () => new Element('div'), analysisClassification: () => null,
    analysisResultSummary: () => null, preprocessingSummary: () => null, analysisNextSteps: () => null,
    contextDetails: () => null, callDetails: () => null, planDetails: () => null,
    analysisTranscript: () => new Element('details'), detailValue: () => {},
    analysisExportButton: () => new Element('button'), cancelAnalysisButton: () => null,
    sourceButton: () => new Element('button'), deleteSourceButton: () => new Element('button')});
  const names = ['textValue', 'node', 'selectedSource', 'detailKey', 'disclosureScope', 'compactText', 'disclosure', 'foldCard',
    'revealSection', 'renderSection', 'statusClass', 'statusLabel', 'pill', 'latency', 'visibleUnits', 'visibleTasks',
    'analysisOperationLabel', 'sourceIdentity', 'renderAnalyses', 'renderHarnesses', 'runtimeGroupIdentity', 'runtimeGroupError',
    'runtimeGroupActive', 'renderDeployments', 'renderUnits', 'renderTasks', 'refreshRevisionDetails', 'revisionAction', 'analysisNextSteps'];
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
    analyses: [{id: 'new', harness_id: 'a', source_version: 'v1', status: 'COMPLETED', started_at: '2026-11-02',
      submitted: {candidate: {entries: [{id: 'run', symbol: 'detect', description: '检测图像真伪'}]}}},
      {id: 'old', harness_id: 'a', status: 'FAILED', started_at: '2026-11-01', error: '原始失败原因'}], units: [],
    runtime_groups: [{id: 'rg-hash-one', source_id: 'a', source_version: 'v1', entries: ['run'], status: 'LOCAL_ONLY', instances: []},
      {id: 'rg-hash-two', source_id: 'b', source_version: 'v2', entries: ['other'], status: 'LOCAL_ONLY', instances: []}]};
}
test('source update option survives refresh per source and is disabled during analysis', () => {
  const {context: c, roots} = load();
  c.state = state();
  c.state.harnesses[0].revision_ref = 'fixture-topic';
  c.renderHarnesses();
  let inputs = roots['harness-list'].querySelectorAll('input');
  assert.equal(inputs.length, 2);
  assert.equal(inputs[0].title, '检查引用：fixture-topic');
  assert.equal(inputs[1].title, '检查默认分支');
  inputs[0].checked = true; inputs[0].onchange();
  c.renderHarnesses();
  inputs = roots['harness-list'].querySelectorAll('input');
  assert.equal(inputs[0].checked, true);
  assert.equal(inputs[1].checked, false);
  c.state.analyses.push({id: 'update', harness_id: 'a', kind: 'SOURCE_UPDATE_ANALYSIS', status: 'RUNNING'});
  c.renderHarnesses();
  inputs = roots['harness-list'].querySelectorAll('input');
  assert.equal(inputs[0].checked, true);
  assert.equal(inputs[0].disabled, true);
  assert.equal(inputs[1].disabled, false);
});
test('source update operation displays installation result and retained version reason', () => {
  const {context: c, roots} = load();
  c.state = state();
  c.state.analyses.unshift({id: 'update', harness_id: 'a', kind: 'SOURCE_UPDATE_ANALYSIS', status: 'COMPLETED',
    update_installation: {status: 'NOT_INSTALLED', reason: '锁忙'}});
  c.renderAnalyses();
  assert.match(roots['analysis-list'].textContent, /新版本未安装，原版本保留。原因：锁忙/);
  c.state.analyses[0].update_installation = {status: 'INSTALLED', analysis_id: 'installed'};
  c.renderAnalyses();
  assert.match(roots['analysis-list'].textContent, /新版本已安装；各入口的检查和运行状态请查看分析结果/);
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
test('all-source operations stay compact, keep errors/actions, and preserve expansion across status changes', () => {
  const {context: c, roots} = load(); c.state = state();
  const render = () => c.renderSection('analyses', c.state, c.renderAnalyses, ['analysis-list']);
  const sources = () => roots['analysis-list'].children.filter(item => (item.className || '').includes('source-disclosure'));
  const records = () => sources().flatMap(source => source.children.filter(item => item.tagName === 'DETAILS'));
  render();
  assert.equal(sources().length, 2); assert.ok(sources().every(source => !source.open));
  assert.match(sources()[0].children[0].textContent, /来源甲.*2 次操作/);
  assert.match(sources()[1].children[0].textContent, /来源乙.*0 次操作/);
  let operations = records();
  assert.equal(operations.length, 3); assert.ok(operations.every(item => !item.open));
  const failed = operations.find(item => item.textContent.includes('原始失败原因'));
  assert.match(failed.children[0].textContent, /失败.*历史记录.*原始失败原因/);
  assert.ok(descendants(failed).some(item => item.tagName === 'BUTTON'));
  sources()[0].open = true; operations[0].open = true;
  operations[0].querySelector('details').open = true;
  c.state.analyses[0].status = 'RUNNING'; c.state.analyses[0].pipeline_stage = 'analysis'; render();
  operations = records();
  assert.equal(sources()[0].open, true); assert.equal(sources()[1].open, false);
  assert.equal(operations[0].open, true); assert.equal(operations[1].open, false);
  assert.equal(operations[0].querySelector('details').open, true);
  assert.match(operations[0].children[0].textContent, /入口与拆分/);
  c.inspectQuery.set('harness', 'a'); c.renderAnalyses();
  assert.equal(sources().length, 1); assert.equal(sources()[0].open, true);
  operations = records();
  assert.equal(operations[0].open, true); assert.equal(operations[1].open, false);
  const revision = descendants(operations[1]).find(item => item.id === 'analysis-revision-old');
  sources()[0].open = false; operations[1].open = false;
  c.revealSection(revision);
  assert.equal(sources()[0].open, true); assert.equal(operations[1].open, true); assert.equal(revision.scrolled, true);
});
test('deployment groups belong to their source and use original purpose/symbol with IDs retained', () => {
  const {context: c, roots} = load(); c.state = state(); c.renderDeployments();
  const sources = roots['deployment-list'].children.filter(item => (item.className || '').includes('source-disclosure'));
  assert.equal(sources.length, 2); assert.ok(sources.every(item => !item.open));
  const first = sources[0].children.find(item => item.tagName === 'DETAILS');
  assert.match(first.children[0].textContent, /来源甲 · detect.*检测图像真伪.*rg-hash-one/);
  assert.doesNotMatch(sources[0].textContent, /rg-hash-two/);
  c.inspectQuery.set('harness', 'a'); c.renderDeployments();
  const selected = roots['deployment-list'].children.find(item => (item.className || '').includes('source-disclosure'));
  assert.equal(selected.open, true); assert.equal(selected.children.find(item => item.tagName === 'DETAILS').open, true);
  assert.equal(c.runtimeGroupError({artifact: {build: {status: 'BUILT', reason: '成功说明'}}}), null);
  c.state.analyses.push({id: 'other-revision', harness_id: 'a', source_version: 'v1',
    submitted: {candidate: {entries: [{id: 'run', symbol: 'unrelated_name', description: '其他修订用途'}]}}});
  const identity = c.runtimeGroupIdentity({...c.state.runtime_groups[0], analysis_id: 'new'});
  assert.match(identity.title, /detect/); assert.doesNotMatch(identity.title, /unrelated_name/);
  assert.equal(identity.purpose, '检测图像真伪');
});
test('deep links reveal every ancestor, and capability edits survive polling while source groups stay collapsed', () => {
  const {context: c, roots} = load(); c.state = state();
  c.state.units = [{id: 'u', entry_id: 'run', name: '图像检测', source_id: 'a', status: 'READY', capability: {goal: 'old'}}];
  c.renderUnits();
  const sourceGroup = roots['unit-list'].children.find(item => item.tagName === 'DETAILS');
  assert.equal(sourceGroup.open, false);
  let textarea = descendants(sourceGroup).find(item => item.tagName === 'TEXTAREA');
  textarea.value = '{"goal":"未保存编辑"}'; textarea.oninput(); c.renderUnits();
  textarea = descendants(roots['unit-list']).find(item => item.tagName === 'TEXTAREA');
  assert.equal(textarea.value, '{"goal":"未保存编辑"}');
  c.revealSection(textarea);
  for (let item = textarea.parentElement; item; item = item.parentElement) if (item.tagName === 'DETAILS') assert.equal(item.open, true);
  assert.equal(textarea.scrolled, true);
});
test('registered sources without groups stay visible with pending-environment reasons and analysis links', () => {
  const {context: c, roots} = load(); c.state = state();
  c.state.harnesses.push({id: 'pending', name: '待重检来源', status: 'ANALYZED', environment_pending_recheck: true,
    target_runtime: {status: 'NOT_PREPARED', reason: '目标解释器需要准备'}});
  c.state.harnesses.push({id: 'new', name: '新来源', status: 'REGISTERED'});
  c.renderDeployments();
  const pending = roots['deployment-list'].children.find(item => item.dataset.detailKey?.endsWith('/deployment-source/pending'));
  assert.equal(pending.open, false);
  assert.match(pending.children[0].textContent, /待重新核对环境 · 当前无可部署运行组.*目标解释器需要准备/);
  assert.ok(descendants(pending).some(item => item.href === '/inspect?view=analyses&harness=pending'));
  assert.match(roots['deployment-list'].textContent, /新来源尚无运行组/);
  c.inspectQuery.set('harness', 'pending'); c.renderDeployments();
  const selected = roots['deployment-list'].children.find(item => item.tagName === 'DETAILS');
  assert.equal(selected.open, true); assert.match(selected.textContent, /待重新核对环境/);
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
