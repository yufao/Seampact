const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/seampact/ui/app.js'), 'utf8');
class Element {
  constructor(tag, text) { this.tag = tag; this.text = text || ''; this.children = []; this.dataset = {}; }
  append(...children) { this.children.push(...children); }
  get textContent() { return this.text + this.children.map(child => child.textContent || '').join(''); }
}
const descendants = root => [root, ...root.children.flatMap(descendants)];
function load() {
  const requests = [], notices = [];
  const context = vm.createContext({
    state: {gpu_devices: [{gpu_uuid:'GPU-observed', name:'Observed GPU'}], deployment_backend:{kind:'docker'}},
    gpuAllocationDrafts: new Map(), renderedSections: new Map(), node: (tag, text) => new Element(tag, text),
    disclosure: (id, title, options) => new Element('details', title + ' · ' + options.meta),
    renderDeployments() {}, refresh: async () => {}, showNotice: value => notices.push(value),
    api: async (url, body) => { requests.push({url, body}); return {}; }});
  for (const name of ['monitorGpuSelectors', 'gpuAllocationDraft', 'probeGpuAllocation', 'saveGpuAllocation', 'renderGpuAllocationControls']) {
    let start = source.indexOf('function ' + name + '(');
    if (source.slice(start - 6, start) === 'async ') start -= 6;
    const rest = source.slice(start), end = rest.search(/\n(?:async )?function /);
    vm.runInContext(rest.slice(0, end), context);
  }
  return {c:context, requests, notices};
}
test('allocation choices share observed identities while edits survive polling', () => {
  const {c} = load(), group = {id:'g', gpu_allocation:{devices:['GPU-assigned'], source:'group'}};
  const draft = c.gpuAllocationDraft(group);
  assert.equal(draft.known.get('GPU-observed').name, 'Observed GPU');
  draft.selected.add('GPU-observed'); draft.dirty = true; draft.extra = '1';
  const same = c.gpuAllocationDraft({...group, gpu_allocation:{devices:[], source:'group'}});
  assert.equal(same, draft); assert.equal(same.extra, '1');
  assert.equal(same.selected.has('GPU-observed'), true);
});
test('probing resolves a selected index to a UUID without silently saving an allocation', async () => {
  const {c, requests} = load(), group = {id:'g', gpu_allocation:{devices:['0']}};
  const draft = c.gpuAllocationDraft(group); draft.extra = '0';
  c.api = async (url, body) => { requests.push({url,body}); return {devices:[{input:'0', gpu_uuid:'GPU-canonical', name:'Resolved'}]}; };
  await c.probeGpuAllocation(group);
  assert.deepEqual(requests.map(request => request.url), ['/api/gpus/probe']);
  assert.deepEqual(Array.from(draft.selected), ['GPU-canonical']);
  assert.equal(draft.known.get('GPU-canonical').name, 'Resolved');
});
test('saving a worker allocation sends explicit UUIDs and supports an empty selection', async () => {
  const {c, requests} = load(), group = {id:'g', gpu_allocation:{devices:['GPU-assigned']}};
  c.gpuAllocationDraft(group).selected.clear();
  await c.saveGpuAllocation(group);
  assert.equal(requests[0].url, '/api/deployments/gpus');
  assert.equal(requests[0].body.group_id, 'g'); assert.equal(requests[0].body.devices.length, 0);
});
test('model service demands must be explicit and never come from observed card capacity', async () => {
  const {c, requests} = load(), group = {id:'service:s', managed_service_id:'s', service_spec:{id:'s', resource_envelope:{devices:{}, basis:'user measurement'}}};
  const draft = c.gpuAllocationDraft(group);
  draft.known.set('GPU-observed', {gpu_uuid:'GPU-observed', memory_total_mib:80000}); draft.selected.add('GPU-observed');
  await c.saveGpuAllocation(group);
  assert.equal(requests.length, 0); assert.match(draft.feedback, /显存需求上界/);
  draft.memory['GPU-observed'] = '12000';
  await c.saveGpuAllocation(group);
  assert.equal(requests[0].url, '/api/managed-services/gpus');
  assert.equal(requests[0].body.memory_mb['GPU-observed'], 12000);
});
test('unreleased instances disable allocation edits while keeping the existing choice visible', () => {
  const {c} = load(), card = new Element('article');
  c.renderGpuAllocationControls(card, {id:'g', gpu_allocation:{devices:['GPU-assigned'], editable:false, edit_reason:'请先释放实例'}, instances:[{state:'READY'}]});
  assert.ok(descendants(card).filter(element => ['input','button'].includes(element.tag)).every(element => element.disabled));
  assert.match(card.textContent, /请先释放实例/);
  assert.equal(descendants(card).find(element => element.value === 'GPU-assigned').checked, true);
});
