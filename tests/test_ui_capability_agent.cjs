const assert = require('node:assert/strict');
const {test} = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/seampact/ui/app.js'), 'utf8');
const html = fs.readFileSync(path.join(__dirname, '../src/seampact/ui/index.html'), 'utf8');

class Element {
  constructor(tag, text = '') { this.tagName = tag; this.text = text; this.children = []; this.dataset = {}; this.value = ''; }
  append(...items) { this.children.push(...items); }
  replaceChildren(...items) { this.children = items; }
  setAttribute() {}
  set textContent(text) { this.text = text; this.children = []; }
  get textContent() { return this.text + this.children.map(item => item.textContent).join(''); }
}
function descendants(root) { return [root, ...root.children.flatMap(descendants)]; }
function load(names, state) {
  const roots = Object.fromEntries(['analysis-list', 'analysis-count', 'harness-list', 'unit-list', 'unit-count'].map(id => [id, new Element('div')]));
  const requests = [];
  const context = vm.createContext({state, inspectPage: false, inspectQuery: new URLSearchParams(),
    $: id => roots[id], selectedSource: () => '', node: (tag, text = '', className) => Object.assign(new Element(tag, text), {className}),
    pill: status => new Element('span', status), empty: (root, text) => root.append(new Element('p', text)),
    detailValue: (root, label, value) => root.append(new Element('pre', JSON.stringify(value))),
    inspectLink: () => new Element('a'), foldCard: card => card, textValue: value => JSON.stringify(value),
    entries: value => value || [], resourceId: item => item.id, resourceState: () => state.resources || {},
    deploymentDrafts: new Map(), renderedSections: new Map(), refresh: async () => {}, showNotice: () => {},
    api: async (url, body) => { requests.push({url, body}); return {}; }});
  for (const name of names) {
    let start = source.indexOf('function ' + name + '('); assert.ok(start >= 0, name);
    if (source.slice(start - 6, start) === 'async ') start -= 6;
    const rest = source.slice(start), end = rest.search(/\n(?:async )?function /);
    vm.runInContext(end < 0 ? rest : rest.slice(0, end), context);
  }
  return {context, roots, requests};
}

test('the page exposes ordinary chat and repository preparation without retired training controls', () => {
  assert.match(html, /id="stop-message"/);
  assert.doesNotMatch(html, /id="research-form"|id="scene-task-selector"|budget-policy/);
  assert.doesNotMatch(source, /\/api\/scheduler\/(?:config|activate)|budget-policy/);
});

test('generation status shows actual gaps and retries the repository through the new endpoint', async () => {
  const {context, roots, requests} = load(['generationPhase', 'generationAction', 'renderAnalyses'], {
    harnesses: [{id: 'source', name: 'Repository'}], generations: [{id: 'generation', source_id: 'source',
      status: 'NEEDS_INPUT', phase: 'preparing', gaps: [{reason: '缺少真实权重'}]}]});
  context.renderAnalyses();
  assert.match(roots['analysis-list'].textContent, /Repository.*NEEDS_INPUT.*准备运行环境.*缺少真实权重/);
  const retry = descendants(roots['analysis-list']).find(item => item.textContent === '重新准备');
  await retry.onclick();
  assert.equal(requests[0].url, '/api/generations');
  assert.equal(requests[0].body.source_id, 'source');
});

test('capability rendering consumes capability contracts and actual availability', () => {
  const {context, roots} = load(['renderUnits'], {capabilities: [{id: 'cap', name: 'File writer', package: 'Repo',
    description: '创建文件', source_id: 'source', availability: {status: 'AVAILABLE'},
    parameters: {type: 'object'}, output: {kind: 'files'}, capability: {id: 'write'}}],
    units: [{id: 'retired', name: 'Old candidate'}]});
  context.renderUnits();
  assert.match(roots['unit-list'].textContent, /File writer.*AVAILABLE.*创建文件/);
  assert.doesNotMatch(roots['unit-list'].textContent, /Old candidate/);
});

test('missing registered weights submit a binding without fabricating a successful call', async () => {
  const {context, requests} = load(['capabilityResourceForm'], {resources: {weights: [{id: 'weights', name: 'Saved model'}]}});
  const root = new Element('div');
  context.capabilityResourceForm(root, {group_id: 'package', resources: [
    {id: 'model', kind: 'weight', description: 'Required checkpoint', required: true, bound: false}]});
  const select = descendants(root).find(item => item.tagName === 'select'); select.value = 'weights';
  const form = descendants(root).find(item => item.tagName === 'form');
  await form.onsubmit({preventDefault() {}});
  assert.equal(requests[0].url, '/api/capabilities/bind');
  assert.deepEqual(JSON.parse(JSON.stringify(requests[0].body)), {group_id: 'package',
    bindings: {model: {form: 'path', resource_id: 'weights', path: '/work/resources/weights'}}});
  assert.equal(requests.length, 1);
});
