// Run with: node --test tests/test_ui_history_paging.cjs
const assert = require('node:assert/strict');
const {test} = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/seampact/ui/app.js'), 'utf8');

function slice(name) {
  const start = source.indexOf('function ' + name + '(');
  assert.ok(start >= 0, name);
  const prefix = source.slice(Math.max(0, start - 6), start) === 'async ' ? 'async ' : '';
  const rest = source.slice(start);
  const end = rest.search(/\n(?:async )?function /);
  return prefix + (end < 0 ? rest : rest.slice(0, end));
}

function load(names, values) {
  const context = vm.createContext(values);
  vm.runInContext(names.map(slice).join('\n'), context);
  return context;
}

function element(tag) {
  const item = {
    tagName: String(tag).toUpperCase(), children: [], className: '', disabled: false, dataset: {},
    text: '', parentElement: null, type: '',
    append(...children) {
      for (const child of children) {
        if (child === undefined || child === null) continue;
        child.parentElement = item;
        item.children.push(child);
      }
    },
    replaceChildren(...children) { item.children = []; item.append(...children); },
    remove() {
      const parent = item.parentElement;
      if (!parent) return;
      parent.children = parent.children.filter(child => child !== item);
      item.parentElement = null;
    },
    setAttribute() {},
    querySelector(selector) {
      const pending = [...item.children];
      while (pending.length) {
        const child = pending.shift();
        const byClass = selector.startsWith('.') && (child.className || '').split(' ').includes(selector.slice(1));
        const byTag = !selector.startsWith('.') && child.tagName === selector.toUpperCase();
        if (byClass || byTag) return child;
        pending.push(...(child.children || []));
      }
      return null;
    },
  };
  Object.defineProperty(item, 'textContent', {
    get() { return item.text + item.children.map(child => child.textContent).join(''); },
    set(value) { item.text = String(value); item.children = []; },
  });
  Object.defineProperty(item, 'childElementCount', {get() { return item.children.length; }});
  Object.defineProperty(item, 'childNodes', {get() { return item.children; }});
  return item;
}

function node(tag, text, className) {
  const item = element(tag);
  if (text !== undefined && text !== null) item.textContent = String(text);
  if (className) item.className = className;
  return item;
}

function taskHarness() {
  const root = element('div');
  const count = element('span');
  const notices = [];
  const calls = [];
  const context = load(['visibleTasks', 'taskRecordFinished', 'loadOlderTasks', 'deleteTaskRecords', 'renderTasks'], {
    state: {tasks: [], analyses: [], units: []},
    olderTasks: [], olderTasksCursor: undefined, olderTasksBusy: false, taskDeleteBusy: false,
    inspectPage: false, inspectQuery: {get() { return null; }},
    terminalStatuses: new Set(['COMPLETED', 'SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED']),
    selectedSource: () => '',
    $: id => id === 'task-list' ? root : count,
    node, empty(target, text) { target.append(node('p', text, 'empty-note')); },
    pill: () => node('span'), latency: () => null, contextDetails: () => null, planDetails: () => null,
    callDetails: () => null, inspectLink: () => node('a', '查看完整详情'), foldCard: card => card,
    textValue: value => String(value ?? ''),
    showNotice: message => { notices.push(message); },
    refresh: async () => {},
    window: {confirm: () => true},
    api: async (url, body) => { calls.push({url, body}); return {deleted: body?.task_ids || []}; },
  });
  return {context, root, count, notices, calls};
}

function buttons(root) { return root.children.filter(child => child.tagName === 'BUTTON'); }

test('visibleTasks keeps state.tasks ahead of older pages and drops duplicate ids', () => {
  const {context: c} = taskHarness();
  c.state = {tasks: [{id: 'live', status: 'RUNNING'}, {id: 'new', status: 'COMPLETED'}], analyses: [], units: []};
  c.olderTasks = [{id: 'new', status: 'FAILED'}, {id: 'old', status: 'COMPLETED'}];
  assert.deepEqual(Array.from(c.visibleTasks(), task => [task.id, task.status]),
    [['live', 'RUNNING'], ['new', 'COMPLETED'], ['old', 'COMPLETED']]);
  c.inspectPage = true;
  c.inspectQuery = {get: key => key === 'task' ? 'old' : ''};
  assert.deepEqual(Array.from(c.visibleTasks(), task => task.id), ['old']);
  c.inspectPage = false;
  c.inspectQuery = {get() { return null; }};
  c.selectedSource = () => 'src';
  c.state = {tasks: [{id: 'a', calls: [{unit: {source_id: 'src'}}]}], analyses: [], units: []};
  c.olderTasks = [{id: 'b', calls: []}, {id: 'c', calls: [{unit: {source_id: 'src'}}]}];
  assert.deepEqual(Array.from(c.visibleTasks(), task => task.id), ['a', 'c']);
});

test('a task pushed off the polled page stays listed once older pages are loaded', () => {
  const c = load(['retainDisplacedTasks'], {olderTasks: [], olderTasksCursor: undefined});
  const previous = [{id: 'c'}, {id: 'b'}, {id: 'a'}];
  // Before any older page is loaded the next cursor still reaches displaced tasks.
  c.retainDisplacedTasks(previous, [{id: 'd'}, {id: 'c'}]);
  assert.deepEqual(Array.from(c.olderTasks), []);
  c.olderTasksCursor = 7;
  c.olderTasks = [{id: 'a'}, {id: 'old'}];
  c.retainDisplacedTasks(previous, [{id: 'd'}, {id: 'c'}]);
  assert.deepEqual(Array.from(c.olderTasks, task => task.id), ['b', 'a', 'old']);
  c.retainDisplacedTasks(undefined, [{id: 'd'}]);
  assert.deepEqual(Array.from(c.olderTasks, task => task.id), ['b', 'a', 'old']);
});

test('loadOlderTasks follows state.tasks_older, then the returned cursor, and stops at null', async () => {
  const {context: c, root, notices} = taskHarness();
  c.state = {tasks: [], analyses: [], units: [], tasks_older: 40};
  c.renderTasks();
  assert.match(root.textContent, /尚无任务/);
  assert.equal(buttons(root).map(button => button.textContent).join(','), '加载更早的任务');
  c.state.tasks = [{id: 'live', status: 'RUNNING', started_at: '9'}];
  const paths = [];
  const pages = [
    {tasks: [{id: 'mid', status: 'COMPLETED', started_at: '5'}, {id: 'live', status: 'FAILED', started_at: '9'}], older: 15},
    {tasks: [{id: 'mid', status: 'COMPLETED', started_at: '5'}, {id: 'early', status: 'COMPLETED', started_at: '1'}], older: null},
  ];
  const loading = [];
  c.api = async url => {
    paths.push(url);
    loading.push(buttons(root).map(button => [button.textContent, button.disabled]));
    return pages[paths.length - 1];
  };
  await c.loadOlderTasks();
  assert.equal(paths[0], '/api/tasks?before=40&limit=30');
  assert.deepEqual(loading[0], [['正在加载…', true]]);
  assert.deepEqual(c.olderTasks.map(task => task.id), ['mid']);
  assert.equal(c.olderTasksCursor, 15);
  assert.deepEqual(buttons(root).map(button => [button.textContent, button.disabled, button.className.includes('secondary-button')]),
    [['加载更早的任务', false, true]]);
  await c.loadOlderTasks();
  assert.equal(paths[1], '/api/tasks?before=15&limit=30');
  assert.deepEqual(c.olderTasks.map(task => task.id), ['mid', 'early']);
  assert.equal(c.olderTasksCursor, null);
  assert.equal(c.olderTasksBusy, false);
  assert.deepEqual(Array.from(c.visibleTasks(), task => [task.id, task.status]),
    [['live', 'RUNNING'], ['mid', 'COMPLETED'], ['early', 'COMPLETED']]);
  assert.equal(buttons(root).length, 0);
  await c.loadOlderTasks();
  assert.equal(paths.length, 2);
  assert.deepEqual(notices, []);
});

test('loadOlderTasks reports a failed page and leaves the cursor unchanged', async () => {
  const {context: c, root, notices} = taskHarness();
  c.state = {tasks: [{id: 'live', status: 'RUNNING', started_at: '9'}], tasks_older: 40, analyses: [], units: []};
  c.api = async () => { throw new Error('列表读取失败'); };
  await c.loadOlderTasks();
  assert.deepEqual(notices, ['列表读取失败']);
  assert.equal(c.olderTasksBusy, false);
  assert.deepEqual(c.olderTasks, []);
  assert.equal(c.olderTasksCursor, undefined);
  assert.deepEqual(buttons(root).map(button => [button.textContent, button.disabled]), [['加载更早的任务', false]]);
});

test('deleteTaskRecords removes deleted ids from olderTasks as well as state.tasks', async () => {
  const {context: c, calls} = taskHarness();
  c.state = {tasks: [{id: 'keep', status: 'COMPLETED'}, {id: 'gone', status: 'COMPLETED'}], analyses: [], units: []};
  c.olderTasks = [{id: 'gone', status: 'COMPLETED'}, {id: 'only-old', status: 'COMPLETED'}, {id: 'old', status: 'COMPLETED'}];
  c.api = async (url, body) => { calls.push({url, body}); return {deleted: ['gone', 'only-old']}; };
  await c.deleteTaskRecords([{id: 'gone', status: 'COMPLETED'}, {id: 'only-old', status: 'COMPLETED'}]);
  assert.equal(calls[0].url, '/api/tasks/delete');
  assert.deepEqual(calls[0].body.task_ids, ['gone', 'only-old']);
  assert.deepEqual(c.state.tasks.map(task => task.id), ['keep']);
  assert.deepEqual(c.olderTasks.map(task => task.id), ['old']);
  assert.equal(c.taskDeleteBusy, false);
});

function researchHarness(api) {
  const run = {id: 'run', status: 'RUNNING', episodes: 1};
  const researchDetails = new Map();
  const context = load(['refreshResearchDetails'], {
    researchDetails, researchDetailVersions: new Map(), researchLoading: new Set(),
    researchSelected: new Set(['run']), researchState: () => ({runs: [run]}),
    runIdentity: item => item.run_id || item.id, renderResearchComparison() {}, api,
  });
  return {context, run, researchDetails};
}

test('research summaries start at 0, append from cached totals, and refetch when the position does not line up', async () => {
  const paths = [];
  const pages = [
    () => ({id: 'run', view: 'summary', status: 'RUNNING', history: [{episode: 1, reward: 0.5}],
      curves: [{episode: 1, reward: 0.5}], history_after: 0, history_total: 1, curves_after: 0, curves_total: 1}),
    () => ({id: 'run', view: 'summary', status: 'RUNNING', history: [{episode: 2, reward: 0.25}],
      curves: [{episode: 2, reward: 0.25}], history_after: 1, history_total: 2, curves_after: 1, curves_total: 2}),
    () => ({id: 'run', view: 'summary', status: 'RUNNING', history: [{episode: 99}],
      curves: [{episode: 99, reward: 9}], history_after: 0, history_total: 9, curves_after: 2, curves_total: 2}),
    () => ({id: 'run', view: 'summary', status: 'RUNNING', history: [{episode: 1}, {episode: 2}, {episode: 3}],
      curves: [{episode: 1, reward: 1}, {episode: 2, reward: 2}, {episode: 3, reward: 3}],
      history_after: 0, history_total: 3, curves_after: 0, curves_total: 3}),
    () => ({id: 'run', view: 'summary', status: 'RUNNING', history: [{episode: 1}, {episode: 2}], curves: [],
      history_after: 3, history_total: 2, curves_after: 3, curves_total: 3}),
    () => ({id: 'run', view: 'summary', status: 'RUNNING', history: [{episode: 7}], curves: [{episode: 7, reward: 7}],
      history_after: 0, history_total: 1, curves_after: 0, curves_total: 1}),
  ];
  const {context: c, run, researchDetails} = researchHarness(async url => { paths.push(url); return pages.shift()(); });
  const episodes = () => Array.from(researchDetails.get('run').history, item => item.episode);
  const rewards = () => Array.from(researchDetails.get('run').curves, item => item.reward);
  await c.refreshResearchDetails();
  assert.equal(paths[0], '/api/scheduler/runs/run?view=summary&history_after=0&curves_after=0');
  assert.deepEqual(episodes(), [1]);
  await c.refreshResearchDetails();
  assert.equal(paths.length, 1);
  run.episodes = 2;
  await c.refreshResearchDetails();
  assert.equal(paths[1], '/api/scheduler/runs/run?view=summary&history_after=1&curves_after=1');
  assert.deepEqual(episodes(), [1, 2]);
  assert.deepEqual(rewards(), [0.5, 0.25]);
  assert.equal(researchDetails.get('run').history_total, 2);
  run.episodes = 3;
  await c.refreshResearchDetails();
  assert.equal(paths[2], '/api/scheduler/runs/run?view=summary&history_after=2&curves_after=2');
  assert.equal(paths[3], '/api/scheduler/runs/run?view=summary&history_after=0&curves_after=0');
  assert.deepEqual(episodes(), [1, 2, 3]);
  run.episodes = 4;
  await c.refreshResearchDetails();
  assert.equal(paths[4], '/api/scheduler/runs/run?view=summary&history_after=3&curves_after=3');
  assert.equal(paths[5], '/api/scheduler/runs/run?view=summary&history_after=0&curves_after=0');
  assert.deepEqual(episodes(), [7]);
  assert.deepEqual(rewards(), [7]);
});

test('an in-flight research refresh does not append the same page twice', async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const paths = [];
  const {context: c, researchDetails} = researchHarness(async url => {
    paths.push(url);
    await gate;
    return {id: 'run', view: 'summary', history: [{episode: 1}, {episode: 1}], curves: [{episode: 1, reward: 1}],
      history_after: 0, history_total: 2, curves_after: 0, curves_total: 1};
  });
  const first = c.refreshResearchDetails();
  const second = c.refreshResearchDetails();
  assert.equal(paths.length, 1);
  release();
  await first;
  await second;
  assert.equal(paths.length, 1);
  assert.deepEqual(researchDetails.get('run').history.map(item => item.episode), [1, 1]);
  assert.equal(c.researchLoading.size, 0);
});

test('summary history still shows resource operation counts and kept feedback metrics', () => {
  const c = load(['finiteNumber', 'displayNumber', 'runMetrics', 'curvePoints', 'jointRunSummary'], {node});
  const run = {
    manifest: {scheduling_mode: 'joint/1', joint: {functional_policy: 'learned', dispatch_policy: 'fifo/1',
      resource_policy: 'fixed/1', capacity: {call_slots: 2, replica_cap: 1}, working_sets: {schema: 'task-working-sets/3'},
      observation: true}},
    joint: {windows: 1, tasks: 1, unfinished_tasks: 0, failed_tasks: 0, max_active_roots: 1, mean_active_roots: [1],
      counters: {'action/invoke': 1}},
    history: [
      {kind: 'joint_window', episode: 1, reward: 0.5, eligible: true, resource_ops_count: 2,
        parallelism: {mean_active_roots: 1, max_active_roots: 1, parallel_fraction: 0.5, task_seconds: 1, instance_seconds: 1},
        service: {arrived: 1, completed: 1}, resource_observation: {status: 'NOT_CONFIGURED'},
        feedback: {reward: 0.5, metrics: [{id: 'lat', status: 'KNOWN', value: 1, denominator: 1}]}},
      {kind: 'joint_window', episode: 2, reward: null, eligible: false, exclusion_reason: '缺失'},
    ],
    curves: [{episode: 1, reward: 0.5}],
  };
  const card = node('div');
  c.jointRunSummary(card, run);
  const rows = [];
  const walk = item => {
    if (item.tagName === 'TR') rows.push(item.children.filter(child => child.tagName === 'TD').map(cell => cell.textContent));
    for (const child of item.children) walk(child);
  };
  walk(card);
  assert.deepEqual(rows.filter(row => row.length === 8).map(row => row[6]), ['2', '0']);
  const metrics = c.runMetrics(run);
  assert.equal(metrics.length, 1);
  assert.equal(metrics[0].id, 'lat');
  assert.equal(metrics[0].value, 1);
  assert.deepEqual(Array.from(c.curvePoints(run), point => [point.x, point.y]), [[1, 0.5]]);
  assert.deepEqual(Array.from(c.curvePoints({history: run.history}), point => point.y), [0.5, null]);
});

test('chat history loads fifty at a time and a new search replaces the list', async () => {
  const root = element('div');
  const search = {value: '图像'};
  const paths = [];
  const all = Array.from({length: 60}, (_, index) => ({id: 'c' + index, title: '会话' + index, status: 'COMPLETED', turns: 1}));
  const context = load(['loadChatHistory'], {
    historySearchVersion: 0, activeChatId: null, pendingTask: null, chatRestoring: false,
    $: id => id === 'chat-history-list' ? root : search,
    node, empty(target, text) { target.append(node('p', text, 'empty-note')); },
    statusLabel: status => status, loadConversation() {}, deleteConversation() {},
    api: async url => {
      paths.push(url);
      const offset = Number(new URL(url, 'http://local').searchParams.get('offset'));
      return {conversations: all.slice(offset, offset + 50), total: all.length};
    },
  });
  const rows = () => root.children.filter(child => (child.className || '').includes('chat-history-row'));
  await context.loadChatHistory();
  assert.equal(paths[0], '/api/chats?q=' + encodeURIComponent('图像') + '&limit=50&offset=0');
  assert.equal(rows().length, 50);
  const more = root.querySelector('.chat-history-more');
  assert.equal(more.textContent, '更多会话');
  assert.equal(more.className.includes('secondary-button'), true);
  await more.onclick();
  assert.equal(paths[1], '/api/chats?q=' + encodeURIComponent('图像') + '&limit=50&offset=50');
  assert.equal(rows().length, 60);
  assert.equal(rows()[0].textContent.includes('会话0'), true);
  assert.equal(rows()[50].textContent.includes('会话50'), true);
  assert.equal(root.querySelector('.chat-history-more'), null);
  search.value = '';
  await context.loadChatHistory();
  assert.equal(paths[2], '/api/chats?q=&limit=50&offset=0');
  assert.equal(rows().length, 50);
  assert.equal(root.querySelector('.chat-history-more').textContent, '更多会话');
});

test('export still requests the full run and the task signature tracks older pages', () => {
  const body = slice('exportResearchRun');
  assert.match(body, /\/api\/scheduler\/runs\/' \+ encodeURIComponent\(identity\)\)/);
  assert.doesNotMatch(body, /view=summary|researchDetails/);
  assert.match(source, /renderSection\('tasks', \[visibleTasks\(\), taskDeleteBusy, olderTasks\.length, olderTasksCursor === undefined \? state\.tasks_older : olderTasksCursor, olderTasksBusy\], renderTasks, \['task-list'\]\)/);
});
