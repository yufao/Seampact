// Run with: node --test tests/test_ui_chat_stream.cjs
const assert = require('node:assert/strict');
const {test} = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

const source = fs.readFileSync(path.join(__dirname, '../src/seampact/ui/app.js'), 'utf8');
function definition(name) {
  const start = source.indexOf('function ' + name + '(');
  assert.ok(start >= 0, name);
  const prefix = source.slice(Math.max(0, start - 6), start) === 'async ' ? 'async ' : '';
  const rest = source.slice(start);
  const end = rest.search(/\n(?:async )?function /);
  return prefix + (end < 0 ? rest : rest.slice(0, end));
}
function setup(batches, task) {
  const writes = [], paths = [], messages = [];
  const output = {body: {}, meta: {}, item: {classList: {add() {}, remove() {}}, scrollIntoView() {}}};
  const context = vm.createContext({
    pendingTask: {id: 'task', output, sceneTaskId: null, partial: ''}, messages,
    state: {tasks: []}, terminalStatuses: new Set(['COMPLETED', 'SUCCEEDED', 'FAILED']),
    $: id => id === 'conversation' ? {scrollHeight: 0, scrollTop: 0, clientHeight: 0} : {hidden: true},
    api: async path => { paths.push(path); return path.endsWith('/task') ? task : batches.shift(); },
    setMessageBody: (_, text) => writes.push(text), renderChatContext() {}, updateComposer() {},
    loadChatHistory() {}, latency: () => '', statusLabel: status => status,
    responseText: text => text || '', textValue: text => String(text), setTimeout,
  });
  vm.runInContext(['followChatEvents', 'synchronizeTask', 'synchronizeTaskRecord'].map(definition).join('\n'), context);
  return {context, output, writes, paths, messages};
}

test('renders deltas once, resets between model requests, and saves only final response', async () => {
  const {context, writes, paths, messages} = setup([
    {events: [{id: 1, type: 'reset'}, {id: 2, type: 'delta', text: 'old'}], done: false},
    {events: [{id: 2, type: 'delta', text: 'old'}, {id: 3, type: 'reset', text: ''},
      {id: 4, type: 'delta', text: 'final'}, {id: 5, type: 'tool', text: 'secret'}], done: false},
    {events: [{id: 6, type: 'delta', text: ' answer'}, {id: 7, type: 'done'}], done: true},
  ], {id: 'task', status: 'COMPLETED', response: 'final answer'});
  await context.followChatEvents(context.pendingTask);
  assert.deepEqual(writes, ['old', 'final', 'final answer', 'final answer']);
  assert.equal(messages.length, 1);
  assert.equal(messages[0].content, 'final answer');
  assert.match(paths[1], /after=2$/);
  assert.match(paths[2], /after=5$/);
  assert.equal(context.pendingTask, null);
});

test('retains partial text as unfinished on failure without adding it to conversation', async () => {
  const {context, output, writes, messages} = setup([
    {events: [{id: 1, type: 'reset', text: 'partial'}, {id: 2, type: 'done'}], done: true},
  ], {id: 'task', status: 'FAILED', error: 'connection lost', partial_response: 'partial'});
  await context.followChatEvents(context.pendingTask);
  assert.equal(writes.at(-1), 'partial');
  assert.match(output.meta.textContent, /未完成的内容.*connection lost/);
  assert.equal(messages.length, 0);
});
