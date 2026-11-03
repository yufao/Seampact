const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../src/seampact/ui/app.js'), 'utf8');
class Element {
  constructor(tag, text) { this.tag = tag; this.text = text || ''; this.children = []; }
  append(...children) { this.children.push(...children); }
  get textContent() { return this.text + this.children.map(c => c.textContent).join(''); }
}
const descendants = root => [root, ...root.children.flatMap(descendants)];
function load(inventory = null, scopes = [], backups = []) {
  const calls = [], confirmations = [];
  const context = vm.createContext({
    node: (tag, text) => new Element(tag, text), disclosure: (id, title) => new Element('details', title),
    state: {image_retention: {settings: {}, scopes, backups}}, imageInventory: inventory,
    imageBusy: false, imageError: '', imageSelected: new Set(), imageDraft: {},
    renderedSections: new Map(), renderDeployments() {}, refresh: async () => {}, showNotice() {},
    window: {confirm: text => { confirmations.push(text); return true; }},
    api: async (url, body) => { calls.push({url, body}); return url === '/api/images' ? inventory : {id: 'new-scope'}; },
  });
  vm.runInContext(source.slice(source.indexOf('function renderImages(root)'), source.indexOf('function renderDeployments()')), context);
  const root = new Element('div'); context.renderImages(root);
  return {root, context, calls, confirmations, button: label => descendants(root).find(c => c.tag === 'button' && c.text === label)};
}

test('image panel never polls Docker during ordinary render and keeps drafts/selections', async () => {
  const first = load();
  assert.equal(first.calls.length, 0);
  assert.match(first.root.textContent, /历史构建成功不能证明/);
  const row = {image_id: 'sha256:example', purposes: ['worker:group'], local_status: 'PRESENT', scopes: [], backups: [], facts: {RepoTags: []}};
  const ui = load({images: [row]});
  const checkbox = descendants(ui.root).find(c => c.tag === 'input' && c.type === 'checkbox');
  checkbox.checked = true; checkbox.onchange();
  assert.equal(ui.context.imageSelected.has(row.image_id), true);
  await ui.button('准备本轮').onclick();
  const call = ui.calls[0];
  assert.equal(call.url, '/api/images/prepare');
  assert.equal(call.body.group_ids[0], 'group');
  assert.equal(call.body.images[0], row.image_id);
  assert.equal(ui.context.imageDraft.scope, 'new-scope');
});

test('archive delete confirms all covered images and remains separate from local deletion', async () => {
  const ui = load({images: []}, [], [{id: 'backup', status: 'VERIFIED_ARCHIVE', path: '/backup/run.tar', images: ['first', 'second'], bytes: 10}]);
  await ui.button('删除整个归档').onclick();
  assert.match(ui.confirmations[0], /first\nsecond/);
  assert.equal(ui.calls[0].body.backup_id, 'backup');
  assert.equal(ui.calls[0].body.images.length, 2);
  assert.equal(ui.calls[0].body.image_id, undefined);
});

test('in-flight launches and preparation failures stay visible with retry-close control', () => {
  const ui = load(null, [{id: 'scope', status: 'CLOSING', images: ['image'], tickets: {launch: {}},
    failure: {reason: 'REFERENCE_MISSING'}, comparison_units: [{id: 'pair', run_ids: ['a', 'b']}]}]);
  assert.match(ui.root.textContent, /REFERENCE_MISSING/);
  assert.match(ui.root.textContent, /在途操作：1/);
  assert.ok(ui.button('结束本轮／重试收尾'));
  assert.equal(ui.button('开始计量'), undefined);
});
