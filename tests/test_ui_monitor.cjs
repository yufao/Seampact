const assert = require('node:assert/strict');
const {test} = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/seampact/ui/app.js'), 'utf8');
function load(name) {
  const start = source.indexOf('function ' + name + '('), rest = source.slice(start);
  const end = rest.search(/\n(?:async )?function /);
  const context = vm.createContext({});
  vm.runInContext(rest.slice(0, end), context);
  return context[name];
}
function functions(names, values) {
  const context = vm.createContext(values);
  for (const name of names) {
    let start = source.indexOf('function ' + name + '(');
    if (source.slice(start - 6, start) === 'async ') start -= 6;
    const rest = source.slice(start), end = rest.search(/\n(?:async )?function /);
    vm.runInContext(rest.slice(0, end), context);
  }
  return context;
}

test('GPU form resolves only entered selectors and retains the draft and saved choices after a failed probe', async () => {
  const inputs = {'monitor-gpu-devices': {value: '0， GPU-example\n1'}, 'monitor-gpu-feedback': {textContent: ''}};
  const calls = [], original = {capabilities: {gpu_devices: [], gpu_uuids: []}};
  const c = functions(['monitorGpuSelectors', 'saveMonitorGpus'], {
    $: id => inputs[id], monitorGpuBusy: false, monitorGpuRevision: 0, monitorGpuHydrated: false,
    monitorGpuDirty: true, monitorSnapshot: original, renderMonitor() {},
    api: async (url, body) => { calls.push({url, body}); throw new Error('GPU 不可见'); }});
  await c.saveMonitorGpus({preventDefault() {}});
  assert.equal(calls.length, 1); assert.equal(calls[0].url, '/api/monitor/gpus');
  assert.deepEqual(Array.from(calls[0].body.devices), ['0', 'GPU-example', '1']);
  assert.equal(inputs['monitor-gpu-devices'].value, '0， GPU-example\n1');
  assert.equal(c.monitorSnapshot, original); assert.equal(c.monitorGpuBusy, false);
  assert.equal(inputs['monitor-gpu-feedback'].textContent, 'GPU 不可见');
});

test('GPU save fills canonical UUIDs and makes verified choices available without starting a session', async () => {
  const inputs = {'monitor-gpu-devices': {value: '0'}, 'monitor-gpu-feedback': {textContent: ''}};
  const device = {gpu_uuid: 'GPU-canonical', index: 0, name: 'Example'};
  const calls = [], snapshot = {capabilities: {gpu_devices: [device], gpu_uuids: [device.gpu_uuid]}};
  const c = functions(['monitorGpuSelectors', 'saveMonitorGpus'], {
    $: id => inputs[id], monitorGpuBusy: false, monitorGpuRevision: 0, monitorGpuHydrated: false,
    monitorGpuDirty: true, monitorSnapshot: {capabilities: {}}, renderMonitor() {},
    api: async (url, body) => { calls.push({url, body}); return body ? {devices: [device], gpu_uuids: [device.gpu_uuid]} : snapshot; }});
  await c.saveMonitorGpus({preventDefault() {}});
  assert.deepEqual(calls.map(call => call.url), ['/api/monitor/gpus', '/api/monitor']);
  assert.equal(inputs['monitor-gpu-devices'].value, device.gpu_uuid);
  assert.equal(c.monitorSnapshot, snapshot); assert.equal(c.monitorGpuDirty, false);
  assert.match(inputs['monitor-gpu-feedback'].textContent, /已识别并保存 1 张/);
});

test('changing saved GPU choices keeps a running session visibly fixed on its original UUIDs', () => {
  class Element {
    constructor(tag, text) { this.tag = tag; this.text = text || ''; this.children = []; }
    append(...children) { this.children.push(...children); }
    replaceChildren() { this.children = []; }
    querySelectorAll() { return this.children.flatMap(child => child.children || []).filter(child => child.tag === 'input' && child.checked); }
    get textContent() { return this.text + this.children.map(child => child.textContent || '').join(''); }
  }
  const roots = Object.fromEntries(['monitor-metrics', 'monitor-gpus', 'monitor-gpu-fieldset', 'monitor-start', 'monitor-stop', 'monitor-current', 'monitor-session'].map(id => [id, new Element('div')]));
  const c = functions(['renderMonitor'], {
    $: id => roots[id], node: (tag, text) => new Element(tag, text),
    document: {querySelectorAll: () => roots['monitor-gpus'].children.flatMap(label => label.children).filter(child => child.tag === 'input')},
    monitorSnapshot: {capabilities: {metrics: [], gpu_uuids: ['GPU-new']},
      current: {id: 'record', status: 'RUNNING', interval_s: 2, gpu_uuids: ['GPU-original']}, sessions: []},
    monitorMetricsSignature: '', monitorBusy: false, monitorGpuBusy: false, monitorSelection: '',
    renderMonitorGpuDevices() {}, renderMonitorRecord() {}, statusLabel: value => value});
  c.renderMonitor();
  const checkboxes = roots['monitor-gpus'].children.flatMap(label => label.children).filter(child => child.tag === 'input');
  assert.equal(checkboxes.find(input => input.value === 'GPU-original').checked, true);
  assert.equal(checkboxes.find(input => input.value === 'GPU-new').checked, false);
  assert.ok(checkboxes.every(input => input.disabled));
  assert.match(roots['monitor-current'].textContent, /本次固定观察 GPU：GPU-original/);
});
test('CPU curves use measured counter rates and separate missing samples and instance generations', () => {
  const series = load('monitorSeries')([
    {monotonic_s: 1, containers: [{scope:'worker_cgroup',instance_id:'w',generation:1,group_id:'g',cpu_seconds:1,cpu_percent:null,memory_current_bytes:1024}]},
    {monotonic_s: 2, containers: [{scope:'worker_cgroup',instance_id:'w',generation:1,group_id:'g',cpu_seconds:9,cpu_percent:200,memory_current_bytes:2048}]},
    {monotonic_s: 3, containers: []},
    {monotonic_s: 4, containers: [{scope:'worker_cgroup',instance_id:'w',generation:2,group_id:'g',cpu_seconds:100,cpu_percent:null,memory_current_bytes:null}]}]);
  const first = series.find(s => s.id === 'worker_cgroup:w:1/cpu');
  assert.equal(first.points[1].y, 2);
  assert.equal(first.points[2].y, null);
  assert.equal(first.points[3].y, null);
  assert.equal(series.find(s => s.id === 'worker_cgroup:w:2/cpu').points[0].y, null);
});
test('device unknowns remain unknown and workload counts are not combined with process counters', () => {
  const series = load('monitorSeries')([{monotonic_s:1,
    processes:[{pid:1,rss_bytes:1048576}], devices:[{gpu_uuid:'GPU-example',memory_used_mib:null,power_draw_w:30}],
    workload:{calls_waiting:2}}]);
  assert.equal(series.find(s => s.id === 'process:1/memory').points[0].y, 1);
  assert.equal(series.find(s => s.id === 'GPU-example/memory_used_mib').points[0].y, null);
  assert.equal(series.find(s => s.id === 'workload/calls_waiting').points[0].y, 2);
});
