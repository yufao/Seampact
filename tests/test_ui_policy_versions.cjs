// Run with: node --test tests/test_ui_policy_versions.cjs
const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/seampact/ui/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../src/seampact/ui/index.html'), 'utf8');

class Element {
  constructor(tag, text = '') { this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.value = ''; this.text = text; this.disabled = false; this.className = ''; }
  append(...items) { for (const item of items) { item.parentElement = this; this.children.push(item); } }
  replaceChildren(...items) { this.children = []; this.text = ''; this.append(...items); }
  set textContent(value) { this.text = String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(item => item.textContent).join(''); }
  setAttribute(name, value) { this[name] = value; }
  querySelectorAll(selector) { return descendants(this).slice(1).filter(item => item.tagName === selector.toUpperCase()); }
  showModal() { this.open = true; }
  close() { this.open = false; this.onclose?.(); }
  remove() { this.removed = true; if (this.parentElement) this.parentElement.children = this.parentElement.children.filter(item => item !== this); }
  focus() { this.focused = true; }
  scrollIntoView() { this.scrolled = true; }
}
function descendants(root) { return [root, ...root.children.flatMap(descendants)]; }
function node(tag, text, className) { const item = new Element(tag, text == null ? '' : String(text)); item.className = className || ''; return item; }
function slice(name) {
  let start = source.indexOf('function ' + name + '('); assert.ok(start >= 0, name);
  if (source.slice(start - 6, start) === 'async ') start -= 6;
  const rest = source.slice(start), end = rest.search(/\n(?:async )?function /);
  return end < 0 ? rest : rest.slice(0, end);
}
const names = ['policyVersionName', 'policyNumber', 'policyDate', 'policyScopeLabel', 'policyMetrics', 'policyStatus', 'policyButton', 'policyOptions',
  'renderPolicyTrainingForm', 'renderPolicyVersions', 'refreshPolicies', 'policyAction', 'selectPolicyForTasks',
  'savePolicyVersion', 'importPolicyVersion', 'preparePolicyBranch', 'policyNumericInput', 'policyTrainingRequest',
  'startPolicyTraining', 'renderPolicyTrainingJobs', 'selectPolicyDetail', 'renderPolicyDetail', 'taskDisplayName',
  'projectDisplayName', 'showTaskActivity'];
function harness(overrides = {}) {
  const roots = new Map(), body = new Element('body'), calls = [], timers = new Map(); let nextTimer = 0;
  const control = id => { if (!roots.has(id)) { const el = new Element('div'); el.id = id; roots.set(id, el); } return roots.get(id); };
  const snapshot = {versions: [], recipes: [{id: 'recipe-one', name: '冻结方案', training_tasks: 20}], jobs: [], busy: {can_switch: true}, active: null};
  control('policy-train-mode').value = 'fresh'; control('policy-train-recipe').value = 'recipe-one';
  const values = {$: control, node, empty(root, text) { root.append(node('p', text)); }, URLSearchParams,
    policySnapshot: snapshot, policySelected: '', policyDetails: new Map(), policyDetailVersion: 0,
    policyLoading: false, policyBusy: false, policyError: '',
    state: {projects: [{id: 'project-one', name: '办公项目'}]},
    document: {body}, statusLabel: value => ({RUNNING: '运行中', COMPLETED: '已完成'}[value] || value),
    detailValue(root, label, value) { root.append(node('details', label + JSON.stringify(value))); },
    inspectLink(view, values) { const link = node('a'); link.href = '/inspect?' + new URLSearchParams({view, ...values}); return link; },
    setTimeout(callback, ms) { const id = ++nextTimer; timers.set(id, {callback, ms}); return id; },
    clearTimeout(id) { timers.delete(id); },
    api: async (url, data) => { calls.push({url, data}); return data ? {id: 'saved'} : snapshot; }, ...overrides};
  const context = vm.createContext(values); vm.runInContext(names.map(slice).join('\n'), context);
  return {c: context, control, roots, body, calls, timers, snapshot};
}
function json(value) { return JSON.parse(JSON.stringify(value)); }
const tick = () => new Promise(resolve => setImmediate(resolve));

test('policy workspace keeps historical research read-only and mounts explicit training actions', () => {
  for (const id of ['policy-save-form', 'policy-import-form', 'policy-train-form', 'policy-current', 'policy-training-list']) assert.match(html, new RegExp('id="' + id + '"'));
  assert.match(html, /<details id="research-archive"[^>]*>\s*<summary>历史研究记录<\/summary>/);
  const research = slice('renderResearch');
  assert.doesNotMatch(research, /policies\/train|training\/resume/);
  assert.match(source, /renderSection\('policies', state.scheduler\?\.policies/);
});

test('version rendering preserves drafts and selectors, shows lineage and never fabricates missing measurements', () => {
  const {c, control, snapshot} = harness();
  snapshot.active = {id: 'parent', name: '基础版本'};
  snapshot.versions = [{id: 'parent', name: '基础版本', kind: 'ppo', created_at: '2026-11-18T01:02:03Z', metrics: {scope: 'training', quality: 0.8, latency_s: 2, ram_byte_s: null, api_tokens: 30}},
    {id: 'child', parent_id: 'parent', name: '<img src=x onerror=alert(1)>', notes: '分支说明', kind: 'ppo', metrics: {scope: 'frozen_development', vram_byte_s: 2 ** 30}}];
  control('policy-save-name').value = '尚未保存的名称'; control('policy-train-name').value = '正在填写的分支';
  control('policy-train-base').value = 'parent'; control('policy-pref-quality').value = '0.4';
  c.renderPolicyVersions(snapshot);
  const root = control('policy-version-list');
  assert.match(root.textContent, /当前使用/); assert.match(root.textContent, /源自：基础版本/); assert.match(root.textContent, /派生分支：<img/);
  assert.match(root.textContent, /未取得/); assert.match(root.textContent, /1 GiB·秒/);
  assert.match(root.textContent, /训练集结果/); assert.match(root.textContent, /冻结开发集评价结果/); assert.doesNotMatch(root.textContent, /T01:02:03Z/);
  assert.equal(descendants(root).filter(item => item.tagName === 'IMG').length, 0);
  c.renderPolicyVersions({...snapshot, busy: {can_switch: false, reason: '任务尚未收尾'}});
  assert.equal(control('policy-save-name').value, '尚未保存的名称'); assert.equal(control('policy-train-name').value, '正在填写的分支');
  assert.equal(control('policy-train-base').value, 'parent'); assert.equal(control('policy-pref-quality').value, '0.4');
  assert.equal(control('policy-train-submit').disabled, true); assert.equal(control('policy-save-submit').disabled, true);
  assert.match(control('policy-current').textContent, /任务尚未收尾/);
  control('policy-filter').value = '分支说明'; c.renderPolicyVersions(); assert.equal(control('policy-version-list').children.length, 1);
  control('policy-filter').value = ''; control('policy-status-filter').value = 'current'; c.renderPolicyVersions();
  assert.equal(control('policy-version-list').children.length, 1); assert.match(control('policy-version-list').textContent, /基础版本/);
});

test('rule details avoid invented training counts and separate source training from frozen development tasks', () => {
  const {c, control} = harness(); c.policySelected = 'rule-one';
  c.policyDetails.set('rule-one', {version: {id: 'rule-one', kind: 'rule', training: {}}, tasks: [
    {id: 'training-one', relation: 'source_training', status: 'COMPLETED'}, {id: 'validation-one', relation: 'frozen_validation', status: 'COMPLETED'}]});
  c.renderPolicyDetail(); const text = control('policy-version-detail').textContent;
  assert.match(text, /规则策略，不包含神经网络训练状态/); assert.doesNotMatch(text, /训练任务 未取得/);
  assert.match(text, /来源实验 · 训练任务/); assert.match(text, /冻结开发集评价任务/);
  assert.equal(c.policyDate('not-a-date'), 'not-a-date');
  assert.doesNotMatch(c.policyDate('2026-11-18T01:02:03Z'), /T|Z/);
});

test('training requests use registered recipes, validate complete positive preferences and preserve resume settings', () => {
  const {c, control, snapshot} = harness(); control('policy-train-name').value = '新训练';
  assert.deepEqual(json(c.policyTrainingRequest()), {name: '新训练', mode: 'fresh', recipe_id: 'recipe-one'});
  control('policy-train-seed').value = '7'; control('policy-train-update').value = '4'; control('policy-train-rate').value = '0.0002';
  for (const key of ['quality', 'latency', 'ram', 'vram', 'api']) control('policy-pref-' + key).value = '0.2';
  control('policy-train-smoothing').value = '0.1';
  const request = json(c.policyTrainingRequest()); assert.equal(request.seed, 7); assert.equal(request.update_after_tasks, 4);
  assert.deepEqual(request.learner_overrides, {learning_rate: 0.0002}); assert.equal(request.objective_overrides.preference.quality, 0.2);
  control('policy-pref-ram').value = '0'; assert.throws(() => c.policyTrainingRequest(), /大于零/);
  control('policy-pref-ram').value = ''; assert.throws(() => c.policyTrainingRequest(), /全部五项/);
  control('policy-pref-ram').value = '0.3'; assert.throws(() => c.policyTrainingRequest(), /等于 1/);
  snapshot.versions.push({id: 'base', kind: 'ppo'}); control('policy-train-base').value = 'base'; control('policy-train-mode').value = 'resume';
  assert.deepEqual(json(c.policyTrainingRequest()), {name: '新训练', mode: 'resume', recipe_id: 'recipe-one', parent_id: 'base'});
  control('policy-train-mode').value = 'branch'; control('policy-pref-ram').value = '0.2';
  assert.equal(c.policyTrainingRequest().parent_id, 'base');
  control('policy-train-recipe').value = 'missing'; assert.throws(() => c.policyTrainingRequest(), /可用的实验方案/);
});

test('failed API actions show the exact error and keep drafts; switching checks current availability', async () => {
  const {c, control, snapshot, calls} = harness(); snapshot.versions = [{id: 'base', kind: 'ppo'}];
  control('policy-save-name').value = '待保存'; control('policy-save-notes').value = '要保留的备注';
  c.api = async (url, data) => { calls.push({url, data}); throw new Error('策略工件摘要不一致'); };
  await c.savePolicyVersion({preventDefault() {}});
  assert.equal(calls[0].url, '/api/policies/save'); assert.deepEqual(json(calls[0].data), {name: '待保存', notes: '要保留的备注'});
  assert.equal(c.policyBusy, false); assert.equal(control('policy-save-name').value, '待保存');
  assert.match(control('policy-feedback').textContent, /策略工件摘要不一致/);
  snapshot.busy.can_switch = false; await c.selectPolicyForTasks('base'); assert.equal(calls.length, 1);
  snapshot.busy.can_switch = true; await c.selectPolicyForTasks('base'); assert.equal(calls[1].url, '/api/policies/select');
});

test('advanced import validates argument arrays and does not turn import into training', async () => {
  const {c, control, calls} = harness(); control('policy-import-protocol').value = '/server/data/plan.json';
  control('policy-import-grader').value = '{"command":"python"}'; await c.importPolicyVersion({preventDefault() {}});
  assert.equal(calls.length, 0); assert.match(control('policy-feedback').textContent, /字符串参数数组/);
  control('policy-import-grader').value = '["python", "grader.py", "{collection}"]'; control('policy-import-name').value = '导入版本';
  await c.importPolicyVersion({preventDefault() {}});
  assert.deepEqual(json(calls[0]), {url: '/api/policies/import', data: {protocol: '/server/data/plan.json', name: '导入版本', grader_command: ['python', 'grader.py', '{collection}']}});
  assert.equal(calls.some(call => call.url === '/api/policies/train'), false);
});

test('paused jobs show real progress, resume explicitly and link their recorded result version', async () => {
  const {c, control, snapshot, calls} = harness();
  snapshot.jobs = [{id: 'job-one', name: '分支', status: 'PAUSED', completed_tasks: 5, planned_tasks: 20, actual_updates: 2, resumable: true, version_id: 'result'}];
  c.renderPolicyTrainingJobs(); const root = control('policy-training-list');
  assert.match(root.textContent, /已完成 5 \/ 20/); assert.match(root.textContent, /策略更新 2 次/);
  const resume = descendants(root).find(item => item.textContent === '恢复训练'); await resume.onclick(); await tick();
  assert.deepEqual(json(calls[0]), {url: '/api/policies/training/resume', data: {job_id: 'job-one'}});
  assert.match(root.textContent, /查看结果版本/);
});

test('failed training offers cleanup despite remaining runtime work, preserves errors and waits for other training', async () => {
  const {c, control, snapshot, calls} = harness(); snapshot.busy.can_switch = false;
  snapshot.jobs = [{id: 'failed-job', name: '失败训练', status: 'FAILED', cleanup_status: 'REQUIRES_DRAIN',
    restore_status: 'REQUIRES_SELECTION', cleanup_error: '实例仍有活动调用', restore_error: '原推理策略尚未恢复'}];
  c.renderPolicyTrainingJobs(); const root = control('policy-training-list');
  assert.match(root.textContent, /实例仍有活动调用/); assert.match(root.textContent, /原推理策略尚未恢复/);
  const cleanup = descendants(root).find(item => item.tagName === 'BUTTON' && item.textContent === '重新收尾'); assert.equal(cleanup.disabled, false);
  cleanup.onclick(); await tick();
  assert.deepEqual(json(calls[0]), {url: '/api/policies/training/cleanup', data: {job_id: 'failed-job'}});
  assert.equal(snapshot.jobs[0].status, 'FAILED'); assert.match(root.textContent, /训练失败/);
  snapshot.jobs.push({id: 'other-job', status: 'RUNNING'}); c.renderPolicyTrainingJobs();
  assert.equal(descendants(root).find(item => item.tagName === 'BUTTON' && item.textContent === '重新收尾').disabled, true);
});

test('a later version selection wins an out-of-order detail response', async () => {
  const {c, control} = harness(); const resolve = new Map();
  c.api = url => new Promise(done => resolve.set(url, done));
  const first = c.selectPolicyDetail('one'), second = c.selectPolicyDetail('two');
  resolve.get('/api/policies/two')({version: {id: 'two', name: '第二版'}, tasks: []}); await second;
  resolve.get('/api/policies/one')({version: {id: 'one', name: '第一版'}, tasks: []}); await first;
  assert.equal(c.policySelected, 'two'); assert.equal(c.policyDetails.has('one'), false);
  assert.match(control('policy-version-detail').textContent, /two/);
});

test('task activity shows policy and true decisions, refreshes active tasks, and stops polling on close', async () => {
  const {c, body, calls, timers} = harness();
  const active = {status: 'RUNNING', project_id: 'project-one', policy_version: {id: 'version-one', name: '基础策略', training: false},
    metrics: {quality: null, latency_s: 0.5, ram_byte_s: null, vram_byte_s: null},
    decisions: [{at: '10:00', event: 'before_delivery', selected: {kind: 'run_hook', hook: '引用检查'}, candidates_count: 2, policy_version: {policy_step_version: 3}}], calls: [], checks: [], instances: []};
  c.api = async url => { calls.push({url}); return active; };
  c.showTaskActivity('task-one'); await tick(); const dialog = body.children[0];
  assert.match(dialog.textContent, /办公项目/);
  assert.doesNotMatch(dialog.textContent, /project-one/);
  const timeline = descendants(dialog).find(item => item.tagName === 'DETAILS' && item.children[0]?.textContent.startsWith('调度决策时间线'));
  timeline.open = true;
  assert.match(dialog.textContent, /基础策略/); assert.match(dialog.textContent, /调度决策时间线/); assert.match(dialog.textContent, /执行检查 · 引用检查/);
  assert.match(dialog.textContent, /2 个可选动作/); assert.match(dialog.textContent, /共享实例/); assert.match(dialog.textContent, /未取得/);
  assert.match(dialog.textContent, /策略更新序号 3/); assert.doesNotMatch(dialog.textContent, /\[object Object\]/);
  assert.equal(timers.size, 1); const timer = [...timers.values()][0]; assert.equal(timer.ms, 2000);
  timers.clear(); timer.callback(); await tick(); assert.equal(calls.length, 2); assert.equal(timers.size, 1);
  assert.equal(timeline.open, true);
  dialog.close(); assert.equal(timers.size, 0); assert.equal(dialog.removed, true);
});

test('activity never schedules another read after closure, even when a request finishes late', async () => {
  const {c, body, timers} = harness(); let finish;
  c.api = () => new Promise(resolve => { finish = resolve; });
  c.showTaskActivity('task-one'); const dialog = body.children[0]; dialog.close();
  finish({status: 'RUNNING', calls: [], checks: [], instances: []}); await tick();
  assert.equal(timers.size, 0); assert.equal(dialog.removed, true);
});

test('finished activity is read-only and does not keep an automatic poll alive', async () => {
  const {c, body, timers} = harness();
  c.api = async () => ({status: 'COMPLETED', calls: [], checks: [], instances: [], decisions: []});
  c.showTaskActivity('old-task'); await tick();
  assert.equal(timers.size, 0); assert.match(body.textContent, /任务已结束/); assert.match(body.textContent, /未保存已登记策略身份/);
  body.children[0].close();
});
