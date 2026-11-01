// Run with: node --test tests/test_ui_analysis_summary.cjs
const assert = require('node:assert/strict');
const {test} = require('node:test');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');

class Element {
  constructor(tag) { this.tagName = tag; this.children = []; this.value = ''; this.open = false; }
  set textContent(value) { this.value = String(value); this.children = []; }
  get textContent() { return this.value + this.children.map(child => child.textContent).join(''); }
  append(...children) { this.children.push(...children); }
}
const source = fs.readFileSync(path.join(__dirname, '../src/seampact/ui/app.js'), 'utf8');
function load() {
  const context = vm.createContext({document: {createElement: tag => new Element(tag)}, inspectPage: true});
  const names = ['textValue', 'detailValue', 'taskRoleLabel', 'entryIssueSummary', 'groupEntryIssues',
    'entryIssuesOverview', 'entryReadinessDetails', 'analysisResultSummary'];
  const definitions = names.map(name => {
    const start = source.indexOf('function ' + name + '(');
    assert.ok(start >= 0, name);
    const rest = source.slice(start);
    const end = rest.search(/\n(?:async )?function /);
    return end < 0 ? rest : rest.slice(0, end);
  });
  vm.runInContext(source.slice(source.indexOf('const node ='), source.indexOf('\nconst messages =')) +
    '\nconst statusLabel = value => value;\n' + definitions.join('\n'), context);
  return context;
}
function descendants(root, predicate) {
  return [root, ...root.children.flatMap(child => descendants(child, predicate))].filter(predicate);
}
function sample() {
  const entries = ['a', 'b', 'c'].map(id => ({id, path: id + '.py', symbol: '__main__', description: '原始任务'}));
  const readiness = entries.map(entry => ({entry_id: entry.id, status: 'NEEDS_REPAIR',
    reason: 'Raw original reason', initialization: 'NOT_RUN', execution: 'NOT_RUN', issues: [
      {issue_code: 'dependency_closure_unsupported', repair_scope: 'rule', target: 'revision', reason: 'dynamic alias'},
      {issue_code: 'dependency_closure_limit', repair_scope: 'rule', target: 'revision', reason: 'finite bound'},
      ...Array.from({length: 9}, (_, index) => ({issue_code: 'configuration_flow_unsupported',
        repair_scope: 'rule', target: 'revision', field_paths: ['dependencies[' + index + '].mappings'],
        dependency_id: 'service-' + index, reason: 'consumer unresolved', source_evidence: [{path: entry.path, start: index + 1}]}))
    ]}));
  return {id: 'review-job', status: 'COMPLETED', submitted: {candidate: {entries}, report: {
    entry_readiness: readiness, entry_bindings: {entries: entries.map(entry => ({...entry, status: 'MISSING', reason: 'Raw original reason'}))}}}};
}

test('33 field and call issues become three groups with one repair link and intact evidence', () => {
  const context = load(), job = sample(), original = JSON.stringify(job);
  const root = context.analysisResultSummary(job);
  const groups = descendants(root, e => e.className === 'entry-issue-group');
  assert.equal(groups.length, 3);
  assert.match(groups[2].textContent, /27 项 · 涉及 3 个入口/);
  assert.equal(descendants(root, e => e.tagName === 'a' && e.href === '#analysis-revision-review-job').length, 1);
  const archived = groups.flatMap(group => descendants(group, e => e.tagName === 'pre')
    .flatMap(pre => JSON.parse(pre.textContent)));
  const expected = job.submitted.report.entry_readiness.flatMap(entry => entry.issues.map(issue => ({...issue, entry_id: entry.entry_id})));
  assert.deepEqual(archived.map(issue => JSON.stringify(issue)).sort(), expected.map(issue => JSON.stringify(issue)).sort());
  assert.equal(JSON.stringify(job), original);
  const details = descendants(root, e => e.className === 'analysis-entry-detail');
  assert.equal(details.length, 3);
  assert.ok(details.every(e => e.open === false));
  const rows = descendants(root, e => e.className === 'analysis-entry-detail-row');
  assert.ok(rows.every(e => e.children[0].colSpan === 3));
  const table = descendants(root, e => e.className === 'research-metrics analysis-entry-table')[0];
  const mainRows = descendants(table, e => e.tagName === 'tr' && e.children[0]?.tagName === 'td' && !e.className);
  assert.equal(mainRows.length, 3);
  assert.ok(mainRows.every(e => e.children[2].textContent.includes('需要平台支持') && !e.children[2].textContent.includes('Raw original reason')));
});

test('different problem kinds and destinations remain distinct, with unique configuration links', () => {
  const context = load();
  const issues = [{category: 'resource_binding', repair_scope: 'resource', target: 'services', dependency_id: 'one'},
    {category: 'resource_binding', repair_scope: 'resource', target: 'services', dependency_id: 'two'},
    {category: 'resource_binding', repair_scope: 'resource', target: 'weights', dependency_id: 'three'},
    {category: 'unsupported_rule', repair_scope: 'rule', target: 'revision', reason: 'first'},
    {category: 'unsupported_rule', repair_scope: 'rule', target: 'revision', reason: 'second'}];
  const root = context.entryIssuesOverview(issues, {id: 'job'});
  assert.equal(descendants(root, e => e.className === 'entry-issue-group').length, 3);
  assert.equal(descendants(root, e => e.tagName === 'a').length, 3);
  assert.equal(context.entryIssuesOverview([], {id: 'job'}), null);
});

test('standalone readiness also groups fields; ready and historical results keep their details', () => {
  const context = load(), job = sample();
  const root = context.entryReadinessDetails(job.submitted.report.entry_readiness[0], job);
  assert.equal(descendants(root, e => e.className === 'entry-issue-group').length, 3);
  assert.equal(descendants(root, e => e.tagName === 'a').length, 1);
  job.submitted.report.entry_readiness = [];
  job.submitted.report.entry_bindings.entries[0].status = 'READY';
  const summary = context.analysisResultSummary(job);
  assert.equal(descendants(summary, e => e.className === 'entry-issue-group').length, 0);
  assert.match(summary.textContent, /待实际运行验证/);
  assert.match(summary.textContent, /Raw original reason/); // Preserved inside original details.
  assert.equal(context.analysisResultSummary({}), null);
});
