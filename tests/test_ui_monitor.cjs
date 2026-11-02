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
