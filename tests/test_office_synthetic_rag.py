import hashlib
import os
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

from seampact.office_synthetic_rag import SyntheticOfficeRagTask, prepare_synthetic_rag_task
from seampact.office_pilot import _read_plan
from seampact.office_experiment import _start_native_task
from seampact.office_benchmark import _install_rag_documents

ROOT = Path(__file__).resolve().parents[1]

class FakeStore:
    def __init__(self): self.rows = {}
    def get(self, kind, identity): return self.rows.get((kind, identity))
    def save(self, kind, identity, data): self.rows[(kind, identity)] = data

class FakeProjects:
    def __init__(self, data): self.data, self.store = Path(data), FakeStore()
    def require_idle(self, project_id): pass
    def project_mount(self, project_id, area): return self.data
    def put_file(self, project_id, cid, area, path, data):
        target = self.data / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    def delete_file(self, project_id, cid, area, path): (self.data / path).unlink()

class SyntheticRagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'var')
        self.addCleanup(self.temp.cleanup)
        output = Path(self.temp.name) / 'fixture'
        subprocess.run([sys.executable, str(ROOT / 'scripts/freeze_office_synthetic_rag.py'), str(output)], check=True, capture_output=True)
        metadata = json.loads((output / 'metadata.json').read_text(encoding='utf-8'))
        self.location = metadata['sources']['office_rag']

    def test_preparation_scoring_and_owned_corpus_switch(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'var') as temp:
            projects = FakeProjects(temp)
            task, info = prepare_synthetic_rag_task(projects, 'project', self.location, 'office-rag:travel')
            task.record_retrieval_receipt(dict(output=dict(index_identity='index', index_reused=False,
                results=[dict(name=info['reference_name'], digest=info['reference_sha256'], content='fare')])) )
            self.assertTrue(task.rag_status()['path_valid'])
            self.assertTrue(task.evaluate('$140')['utility'])
            old = set(info['corpus_digests'])
            task, info = prepare_synthetic_rag_task(projects, 'project', self.location, 'office-rag:room')
            self.assertFalse(any((Path(temp) / name).exists() for name in old - set(info['corpus_digests'])))
            self.assertFalse(task.evaluate('not birch')['utility'])
            (Path(temp) / info['reference_name']).write_text('modified', encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'modified'):
                prepare_synthetic_rag_task(projects, 'project', self.location, 'office-rag:access')

    def test_real_admission_passes_retrieval_and_scorer_to_chat(self):
        class Chat:
            def start(self, request, **kwargs):
                self.request, self.options, self.event = request, kwargs['runtime_options'], kwargs['on_event']
                return dict(task_id='task')
        class Platform: pass
        with tempfile.TemporaryDirectory(dir=ROOT / 'var') as temp:
            platform = Platform(); platform.projects = FakeProjects(temp); platform.chat = Chat()
            location = self.location
            body = dict(manifest=location['manifest'], manifest_sha256=location['manifest_sha256'],
                case_id='office-rag:travel', project_id='project', synthetic_rag=location,
                rag=dict(corpus_dir='unused', corpus_sha256='unused', capability_id='retriever'))
            unit = dict(id='retriever', tool_name='search', package_digest='package')
            result = _start_native_task(platform, body, capability_snapshot=[unit],
                project_tools=[dict(function=dict(name='search'))])
            self.assertEqual(result['task_id'], 'task')
            self.assertEqual(platform.chat.options['allowed_capabilities'], ['retriever'])
            marker = json.loads((Path(temp) / 'office-reference/.seampact-rag-owner.json').read_text())
            name, digest = next(iter(marker['files'].items()))
            platform.chat.event(dict(kind='tool_receipt', name='search', result=dict(output=dict(
                index_identity='index', index_reused=False, results=[dict(name=name, digest=digest, content='fare')]))))
            score = platform.chat.options['evaluator'](dict(status='COMPLETED', final_response='140'))
            self.assertTrue(score['metrics']['utility'])
            self.assertTrue(score['retrieval']['path_valid'])

    def test_forged_ownership_cannot_remove_unknown_files(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'var') as temp:
            data = Path(temp); projects = FakeProjects(data)
            marker = data / 'office-reference/.seampact-rag-owner.json'
            marker.parent.mkdir()
            unrelated = data / 'shared.txt'; unrelated.write_text('keep', encoding='utf-8')
            unrelated_hash = hashlib.sha256(unrelated.read_bytes()).hexdigest()
            for name in ('shared.txt', '../shared.txt', 'office-reference/.seampact-rag-owner.json',
                         'office-reference/unrelated.txt', 'office-reference/other.md'):
                marker.write_text(json.dumps(dict(schema='seampact.office-rag-project/2', files={name: unrelated_hash})))
                with self.subTest(name=name), self.assertRaises(ValueError):
                    prepare_synthetic_rag_task(projects, 'project', self.location, 'office-rag:travel')
                self.assertEqual(unrelated.read_text(), 'keep')
            # Even a syntactically valid source name and forged matching digest
            # cannot authorize replacing arbitrary content.
            known_name = data / 'office-reference/purchase-1.txt'
            known_name.write_text('private data', encoding='utf-8')
            marker.write_text(json.dumps(dict(schema='seampact.office-rag-project/2', files={
                'office-reference/purchase-1.txt': hashlib.sha256(known_name.read_bytes()).hexdigest()})))
            with self.assertRaisesRegex(ValueError, 'trusted ownership ledger'):
                prepare_synthetic_rag_task(projects, 'project', self.location, 'office-rag:travel')
            self.assertEqual(known_name.read_text(), 'private data')

    def test_installed_variant_can_switch_back_without_allowing_marker_forgery(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'var') as temp:
            data = Path(temp); projects = FakeProjects(data)
            name = 'office-reference/drive-0.txt'
            first, second = b'ordinary document variant one', b'ordinary document variant two'
            digest = lambda value: hashlib.sha256(value).hexdigest()
            _install_rag_documents(projects, 'project', {name: first}, {name: digest(first)})
            _install_rag_documents(projects, 'project', {name: second}, {name: digest(second)})
            self.assertEqual((data / name).read_bytes(), second)
            # A model-writable marker cannot invent permission to remove another file.
            victim_name = 'office-reference/victim.txt'
            victim = data / victim_name; victim.write_text('keep', encoding='utf-8')
            marker = data / 'office-reference/.seampact-rag-owner.json'
            marker.write_text(json.dumps(dict(schema='seampact.office-rag-project/2', files={
                name: digest(second), victim_name: digest(victim.read_bytes())})))
            with self.assertRaisesRegex(ValueError, 'trusted ownership ledger'):
                _install_rag_documents(projects, 'project', {name: first}, {name: digest(first)})
            self.assertEqual(victim.read_text(), 'keep')
            self.assertEqual((data / name).read_bytes(), second)

    def test_owned_hardlink_is_never_replaced(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'var') as temp:
            data = Path(temp); projects = FakeProjects(data)
            task, info = prepare_synthetic_rag_task(projects, 'project', self.location, 'office-rag:travel')
            document = data / info['reference_name']
            link = data / 'outside-copy'
            try:
                os.link(document, link)
            except OSError as error:
                self.skipTest(f'hardlinks unavailable: {error}')
            before = link.read_bytes()
            with self.assertRaises(ValueError):
                prepare_synthetic_rag_task(projects, 'project', self.location, 'office-rag:room')
            self.assertEqual(link.read_bytes(), before)
            self.assertTrue(document.exists())

    def test_currency_forms_only_apply_to_explicit_dollar_questions(self):
        money = dict(id='money', prompt='Give the amount in dollars.')
        count = dict(id='count', prompt='How many headsets remain?')
        for answer in ('140', '$140', '$ 140', '$140.00', '140 dollars', '140 USD', 'USD 140'):
            with self.subTest(answer=answer):
                self.assertTrue(SyntheticOfficeRagTask(money, [['140']]).evaluate(answer)['utility'])
        self.assertTrue(SyntheticOfficeRagTask(count, [['35']]).evaluate('35')['utility'])
        self.assertFalse(SyntheticOfficeRagTask(count, [['35']]).evaluate('$35')['utility'])

    def test_source_plan_and_family_separation(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'var') as temp:
            root = Path(temp)
            dojo = root / 'dojo.json'
            raw = json.dumps(dict(cases=[])).encode()
            dojo.write_bytes(raw)
            plan = dict(schema='seampact.office-comparison/1', manifest=str(dojo), manifest_sha256=hashlib.sha256(raw).hexdigest(),
                source_manifests=dict(office_rag=self.location), cases=dict(training=['office-rag:purchase'],
                development=['office-rag:version'], evaluation=['office-rag:survey']), scheduler={}, training_batch_size=1)
            raw = json.dumps(plan).encode()
            path = root / 'plan.json'; path.write_bytes(raw)
            _, _, cases = _read_plan(dict(protocol=str(path), protocol_sha256=hashlib.sha256(raw).hexdigest()))
            self.assertEqual(cases['office-rag:survey']['_source'], 'office_rag')

if __name__ == '__main__': unittest.main()
