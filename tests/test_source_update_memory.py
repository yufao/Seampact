"""Source revision materials and execution evidence; no model or remote calls."""

import copy
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from seampact.analysis import content_digest, preparation_input
from seampact.configuration.dependencies import Dependencies, DependencyBindings, _execution_basis
from seampact.source.inspection import Inspector
from seampact.intake import RepositoryIntake
from seampact.analysis_support.revisions import build_review_context
from seampact.service import Platform
from seampact.service import RULESET, NATIVE_PROFILE
from seampact.store import Store


class SourceUpdateMemoryChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name)
        self.root = self.home / "original"
        self.root.mkdir()
        self.store = Store(self.home / "platform.sqlite")
        self.addCleanup(self.store.close)
        self.intake = RepositoryIntake(self.home / "managed")
        self.platform = Platform.__new__(Platform)
        self.platform.home = self.home
        self.platform.store = self.store
        self.platform.source_lock = threading.RLock()
        self.implementation = {"inspection.py": "current-checker"}
        self.platform.analysis = SimpleNamespace(freeze_implementation=lambda inspector: self.implementation)

    def write(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8", newline="\n")

    def freeze(self, identity):
        source = self.intake.prepare(str(self.root))
        source["id"] = "source"
        inspector = Inspector.freeze_repository(source, self.home / "snapshots", identity, "original observation")
        return source, inspector

    def test_refresh_discovers_additions_deletions_and_keeps_old_frozen_bytes(self):
        self.write("changed.py", "def original():\n    return 1\n")
        self.write("deleted.py", "def removed():\n    return 2\n")
        self.write("stable.py", "import changed\n")
        _, before = self.freeze("before")
        build_review_context(before, before, self.store)
        self.write("changed.py", "def replacement():\n    return 3\n")
        (self.root / "deleted.py").unlink()
        self.write("added.py", "def main():\n    return 4\n")
        _, after = self.freeze("after")
        context = build_review_context(before, after, self.store)
        self.assertEqual(context["source_diff"]["added"], ["added.py"])
        self.assertEqual(context["source_diff"]["removed"], ["deleted.py"])
        self.assertEqual(context["source_diff"]["modified"], ["changed.py"])
        self.assertEqual(context["reuse"]["hits"], 1)
        names = {symbol["symbol"] for fact in context["facts"] for symbol in fact.get("symbols", [])}
        self.assertIn("main", names)
        self.assertIn("replacement", names)
        self.assertEqual(before.data("deleted.py"), b"def removed():\n    return 2\n")
        self.assertIn(b"original", before.data("changed.py"))
        self.assertIn(b"replacement", after.data("changed.py"))
        self.assertNotEqual(before.request["source_version"], after.request["source_version"])
        reference = next(item for item in context["related_candidates"] if item["path"] == "stable.py")
        self.assertEqual(reference["sha256"], after.files["stable.py"]["sha256"])
        self.assertEqual(context["mappings"], "REQUIRE_CURRENT_CHECK")
        self.assertEqual(context["resource_selections"], "REQUIRE_CURRENT_CHECK")

    def revision(self):
        self.write("original.py", "def main():\n    return 1\n")
        source, inspector = self.freeze("base-request")
        self.store.create("request", inspector.request["id"], inspector.request)
        self.store.create("analysis", "base", dict(id="base", request_id=inspector.request["id"]))
        materials = build_review_context(inspector, inspector, self.store)
        materials["basis"] = self.platform._revision_material_basis(inspector, self.implementation)
        candidate = dict(entries=[], runtime_groups=[])
        revision = dict(id="r", harness_id="source", base_analysis_id="base", proposed_source=source,
            request_id=inspector.request["id"],
            source_version=inspector.request["source_version"], materials=materials,
            implementation=copy.deepcopy(self.implementation), status="CHECKED", report={"status": "READY"},
            checked_digest=content_digest(candidate), candidate=candidate, candidate_digest=content_digest(candidate),
            base_candidate=copy.deepcopy(candidate), provenance=[], result={"status": "succeeded"},
            result_basis={"source_version": inspector.request["source_version"]}, check_identity={"old": True})
        return revision, inspector

    def test_resource_change_rebinds_materials_and_invalidates_old_check(self):
        revision, inspector = self.revision()
        old_basis = copy.deepcopy(revision["materials"]["basis"])
        inspector.request["resource_catalog"] = {"weights": [{"id": "selected", "config_digest": "new"}]}
        self.platform._refresh_revision_scope(revision, inspector)
        basis = revision["materials"]["basis"]
        self.assertEqual(basis["source_version"], old_basis["source_version"])
        self.assertNotEqual(basis["input_digest"], old_basis["input_digest"])
        self.assertEqual(basis["input_digest"], content_digest(preparation_input(inspector)))
        self.assertEqual(revision["materials"]["reuse"]["hits"], 1)
        self.assertIsNone(revision["report"])
        self.assertIsNone(revision["checked_digest"])
        self.assertNotIn("check_identity", revision)
        self.assertEqual(revision["result"]["status"], "unknown")
        self.assertEqual(revision["status"], "DRAFT")

    def test_current_model_report_survives_expanded_material_scope(self):
        revision, inspector = self.revision()
        revision["analysis_id"] = "model"
        inspector.request["configuration"] = {"entries": {"main": {"fixed_arguments": {"value": 2}}}}
        self.store.create("analysis_revision", "r", revision)
        report = {"status": "CURRENT_CHECK"}
        job = dict(id="model", revision_id="r", implementation=self.implementation,
            submitted=dict(candidate=revision["candidate"], report=report))
        self.platform._complete_revision(inspector, job)
        saved = self.store.get("analysis_revision", "r")
        self.assertEqual(saved["status"], "CHECKED")
        self.assertEqual(saved["report"], report)
        self.assertEqual(saved["checked_digest"], saved["candidate_digest"])
        self.assertEqual(saved["check_identity"]["input_digest"], saved["materials"]["basis"]["input_digest"])

    def test_implementation_change_invalidates_materials_even_for_unchanged_source(self):
        revision, inspector = self.revision()
        self.implementation["inspection.py"] = "new-checker"
        self.platform._refresh_revision_scope(revision, inspector)
        self.assertEqual(revision["materials"]["basis"]["implementation"], self.implementation)
        self.assertIsNone(revision["checked_digest"])

    def test_update_keeps_complete_candidate_without_inheriting_ready_or_installing(self):
        self.write("original.py", "def main():\n    return 1\n")
        source, inspector = self.freeze("base-request")
        candidate = dict(entries=[dict(id="one"), dict(id="two")], runtime_groups=[dict(entries=["one", "two"])])
        source.update(analysis_id="base", request_id=inspector.request["id"], status="ANALYZED")
        self.store.create("request", inspector.request["id"], inspector.request)
        self.store.create("harness", "source", source)
        self.store.create("analysis", "base", dict(id="base", request_id=inspector.request["id"], status="COMPLETED",
            submitted=dict(candidate=candidate, report={"status": "READY"})))
        self.platform.intake = self.intake
        self.platform.dependencies = SimpleNamespace(selections=lambda source_id: {})
        self.platform.resource_catalog = lambda: {}
        self.write("original.py", "def main():\n    return 2\n")
        self.write("new_capability.py", "def main():\n    return 3\n")
        created = self.platform.create_revision(dict(analysis_id="base", mode="update"))["revision"]
        self.assertEqual(created["candidate"], candidate)
        self.assertEqual(created["status"], "DRAFT")
        self.assertIsNone(created["checked_digest"])
        self.assertIsNone(created["report"])
        self.assertNotEqual(created["source_version"], inspector.request["source_version"])
        self.assertIn("new_capability.py", created["source_diff"]["added"])
        self.assertEqual(self.store.get("harness", "source")["analysis_id"], "base")
        self.assertEqual(self.store.get("analysis", "base")["submitted"]["report"], {"status": "READY"})

    def test_update_preserves_requested_git_ref_and_allows_explicit_change(self):
        def git(*args):
            return subprocess.run(['git', '-C', str(self.root), *args], check=True,
                                  capture_output=True, text=True).stdout.strip()
        git('init')
        git('config', 'user.name', 'Fixture')
        git('config', 'user.email', 'fixture@example.invalid')
        self.write('original.py', 'def main():\n    return 1\n')
        git('add', 'original.py')
        git('commit', '-m', 'initial')
        default_ref = git('branch', '--show-current')
        git('checkout', '-b', 'fixture-topic')
        self.write('original.py', 'def main():\n    return 2\n')
        git('commit', '-am', 'topic')
        source = self.intake.prepare(self.root.as_uri(), 'fixture-topic')
        self.assertEqual(source['revision_ref'], 'fixture-topic')
        self.assertNotEqual(source['revision'], default_ref)
        source['id'] = 'source'
        inspector = Inspector.freeze_repository(source, self.home / 'snapshots', 'base-request', 'observation')
        source.update(analysis_id='base', request_id=inspector.request['id'], status='ANALYZED')
        self.store.create('request', inspector.request['id'], inspector.request)
        self.store.create('harness', 'source', source)
        self.store.create('analysis', 'base', dict(id='base', request_id=inspector.request['id'], status='COMPLETED',
            submitted=dict(candidate=dict(entries=[], runtime_groups=[]), report={'status': 'READY'})))
        self.platform.intake = self.intake
        self.platform.dependencies = SimpleNamespace(selections=lambda source_id: {})
        self.platform.resource_catalog = lambda: {}
        self.write('original.py', 'def main():\n    return 3\n')
        git('commit', '-am', 'topic update')
        implicit = self.platform.create_revision(dict(analysis_id='base', mode='update'))['revision']
        proposed = self.store.get('analysis_revision', implicit['id'])['proposed_source']
        self.assertEqual(proposed['revision_ref'], 'fixture-topic')
        self.assertEqual(proposed['revision'], git('rev-parse', 'fixture-topic'))
        self.assertIn('original.py', implicit['source_diff']['modified'])
        explicit = self.platform.create_revision(dict(analysis_id='base', mode='update', revision=default_ref))['revision']
        proposed = self.store.get('analysis_revision', explicit['id'])['proposed_source']
        self.assertEqual(proposed['revision_ref'], default_ref)
        self.assertEqual(proposed['revision'], git('rev-parse', default_ref))
        default_source = self.intake.prepare(self.root.as_uri())
        self.assertIsNone(default_source['revision_ref'])

    def test_check_update_skips_unchanged_without_model_call(self):
        revision, inspector = self.revision()
        source = revision["proposed_source"]
        source.update(analysis_id="base", request_id=inspector.request["id"], status="ANALYZED")
        self.store.save("harness", "source", source)
        base = self.store.get("analysis", "base")
        base.update(status="COMPLETED", submitted={"candidate": revision["candidate"]})
        self.store.save("analysis", "base", base)
        self.platform.intake = self.intake
        self.platform.dependencies = SimpleNamespace(selections=lambda source_id: {})
        self.platform.resource_catalog = lambda: {}
        self.platform.analysis.start = lambda *a, **kw: self.fail("unchanged source called model")
        self.assertEqual(self.platform.analyze_update("source")["update_status"], "UNCHANGED")
        self.assertEqual(self.store.get("harness", "source")["analysis_id"], "base")

    def test_first_update_reuses_original_reference(self):
        self.write('original.py', 'pass\n')
        source, _ = self.freeze('first')
        source.update(status='CONNECTED', revision_ref='fixture-topic')
        self.store.save('harness', 'source', source)
        self.platform.intake = self.intake
        with patch.object(self.intake, 'prepare', return_value=source) as prepare, \
                patch.object(self.platform, '_analyze', return_value={'analysis_id': 'new'}):
            self.assertEqual(self.platform.analyze_update('source')['update_status'], 'FIRST_ANALYSIS')
        self.assertEqual(prepare.call_args.args[1], 'fixture-topic')

    def test_changed_update_starts_new_discovery_and_keeps_old_source(self):
        revision, inspector = self.revision()
        source = revision["proposed_source"]
        source.update(analysis_id="base", request_id=inspector.request["id"], status="ANALYZED")
        self.store.save("harness", "source", source)
        base = self.store.get("analysis", "base")
        base.update(status="COMPLETED", submitted={"candidate": revision["candidate"]})
        self.store.save("analysis", "base", base)
        self.platform.intake = self.intake
        self.platform.dependencies = SimpleNamespace(selections=lambda source_id: {})
        self.platform.resource_catalog = lambda: {}
        self.platform.inspectors = {}
        self.platform.profiles = SimpleNamespace(role_identity=lambda role: role)
        calls = []
        self.platform.analysis.start = lambda *args, **kwargs: calls.append(kwargs)
        self.write("original.py", "def main():\n    return 2\n")
        with patch.object(self.platform, "_revision_inspector", side_effect=lambda draft:
                          Inspector(draft["proposed_source"]["root"], self.home / "snapshots",
                                    self.store.get("request", draft["request_id"]))):
            result = self.platform.analyze_update("source")
        self.assertEqual(result["update_status"], "ANALYZING")
        self.assertEqual(calls[0]["preprocessing"], "api")
        self.assertEqual(calls[0]["staging_revision"], result["revision_id"])
        self.assertNotIn("repair", calls[0])
        self.assertEqual(self.store.get("harness", "source")["analysis_id"], "base")

    def test_update_with_no_checked_entry_cannot_replace_original(self):
        self.write("original.py", "def main():\n    pass\n")
        source, inspector = self.freeze("update-request")
        source.update(status="ANALYZED", analysis_id="base")
        self.store.save("harness", "source", source)
        job = dict(id="update", revision_id="r", kind="SOURCE_UPDATE_ANALYSIS", status="COMPLETED",
                   completion_scope="candidate_and_report_only", submitted={"report": {"entry_bindings": {"has_bindable_entries": False}}})
        self.store.save("analysis", job["id"], job)
        self.platform.analysis.cancels = {}
        with patch.object(self.platform, "_complete_revision"), patch.object(self.platform, "install_revision") as install:
            self.assertEqual(self.platform._connect_units(inspector, job), [])
        install.assert_not_called()
        self.assertEqual(self.store.get("harness", "source")["analysis_id"], "base")
        self.assertEqual(job["update_installation"]["status"], "NOT_INSTALLED")

    def test_update_install_failure_keeps_original_and_records_reason(self):
        self.write("original.py", "def main():\n    pass\n")
        source, inspector = self.freeze("update-request")
        source.update(status="ANALYZED", analysis_id="base")
        self.store.save("harness", "source", source)
        job = dict(id="update", revision_id="r", kind="SOURCE_UPDATE_ANALYSIS", status="COMPLETED",
                   completion_scope="candidate_and_report_only", submitted={"report": {"entry_bindings": {"has_bindable_entries": True}}})
        self.store.save("analysis", job["id"], job)
        self.platform.analysis.cancels = {}
        with patch.object(self.platform, "_complete_revision"), patch.object(self.platform, "install_revision", side_effect=ValueError("锁忙")) as install:
            self.assertEqual(self.platform._connect_units(inspector, job), [])
        self.assertTrue(install.call_args.args[0]["require_bindable"])
        self.assertEqual(self.store.get("harness", "source")["analysis_id"], "base")
        self.assertEqual(job["update_installation"]["status"], "NOT_INSTALLED")

    def test_real_update_install_preserves_old_history_and_frozen_source(self):
        self.write('original.py', 'def main(value: str) -> str:\n    return "old:" + value\n')
        platform = Platform(self.home / 'actual-platform')
        self.addCleanup(platform.store.close)
        self.addCleanup(platform.runtime.close)
        identity = platform.add_harness({'address': str(self.root)})['id']
        source = platform.store.get('harness', identity)
        before = Inspector.freeze_repository(source, platform.home / 'snapshots', 'original-request', 'original observation')
        before.request.update(rules=copy.deepcopy(RULESET), entry_profile=copy.deepcopy(NATIVE_PROFILE),
            resource_catalog=platform.resource_catalog(), dependency_bindings={})
        candidate = dict(entries=[dict(id='main', kind='function', path='original.py', symbol='main')],
                         boundaries=[], dependencies=[], runtime_groups=[dict(id='whole', entries=['main'])])
        report = platform.analysis._check_candidate(before, candidate)
        self.assertTrue(report['entry_bindings']['has_bindable_entries'])
        platform.store.save('request', before.request['id'], before.request)
        old_job = dict(id='original-analysis', harness_id=identity, request_id=before.request['id'], status='COMPLETED',
                       submitted=dict(candidate=copy.deepcopy(candidate), report=copy.deepcopy(report)),
                       implementation=platform.analysis.freeze_implementation(before), messages=[], usage=[], model_calls=0)
        platform.store.save('analysis', old_job['id'], old_job)
        source.update(status='ANALYZED', analysis_id=old_job['id'], request_id=before.request['id'], installation_id=old_job['id'])
        platform.store.save('harness', identity, source)
        platform.dependencies.record(before, candidate)
        old_units = platform.catalog.install(before, candidate, old_job['id'])
        self.assertTrue(any(unit['status'] == 'READY' for unit in old_units))
        old_snapshot = copy.deepcopy(platform.store.get('analysis', old_job['id']))
        old_bytes = before.data('original.py')
        self.write('original.py', 'def main(value: str) -> str:\n    return "new:" + value\n')
        with patch.object(platform.analysis, 'start') as start:
            platform.profiles.role_identity = lambda role: role
            update = platform.analyze_update(identity)
        self.assertEqual(update['update_status'], 'ANALYZING')
        updated_inspector = start.call_args.args[0]
        current_report = platform.analysis._check_candidate(updated_inspector, candidate)
        self.assertTrue(current_report['entry_bindings']['has_bindable_entries'])
        job = dict(id=update['analysis_id'], revision_id=update['revision_id'], harness_id=identity,
            kind='SOURCE_UPDATE_ANALYSIS', completion_scope='candidate_and_report_only', status='COMPLETED',
            submitted=dict(candidate=copy.deepcopy(candidate), report=current_report),
            implementation=platform.analysis.freeze_implementation(updated_inspector), messages=[], usage=[], model_calls=0)
        platform.store.save('analysis', job['id'], job)
        units = platform._connect_units(updated_inspector, job)
        self.assertEqual(job['update_installation']['status'], 'INSTALLED', job['update_installation'])
        current = platform.store.get('harness', identity)
        self.assertEqual(current['analysis_id'], job['update_installation']['analysis_id'])
        self.assertEqual(current['installation_id'], current['analysis_id'])
        self.assertTrue(units)
        visible = [unit for unit in platform.catalog.public() if unit['id'] in units]
        self.assertEqual(len(visible), 1)
        self.assertEqual(visible[0]['status'], 'READY')
        self.assertEqual(platform.catalog.invoke(visible[0]['id'], {'value': 'fixture'}), 'new:fixture')
        installed = platform.store.get('analysis', current['analysis_id'])
        self.assertEqual(installed['submitted']['candidate'], candidate)
        self.assertEqual(platform.store.get('request', installed['request_id'])['source_version'], updated_inspector.request['source_version'])
        self.assertNotEqual(updated_inspector.request['source_version'], before.request['source_version'])
        self.assertEqual(platform.store.get('analysis', old_job['id']), old_snapshot)
        self.assertEqual(before.data('original.py'), old_bytes)


    def test_real_update_partial_install_failure_keeps_old_unit_callable(self):
        self.write('original.py', 'def main(value: str) -> str:\n    return "old:" + value\n')
        platform = Platform(self.home / 'actual-platform')
        self.addCleanup(platform.store.close)
        self.addCleanup(platform.runtime.close)
        identity = platform.add_harness({'address': str(self.root)})['id']
        source = platform.store.get('harness', identity)
        before = Inspector.freeze_repository(source, platform.home / 'snapshots', 'original-request', 'original observation')
        before.request.update(rules=copy.deepcopy(RULESET), entry_profile=copy.deepcopy(NATIVE_PROFILE),
            resource_catalog=platform.resource_catalog(), dependency_bindings={})
        candidate = dict(entries=[dict(id='main', kind='function', path='original.py', symbol='main')],
                         boundaries=[], dependencies=[], runtime_groups=[dict(id='whole', entries=['main'])])
        report = platform.analysis._check_candidate(before, candidate)
        self.assertTrue(report['entry_bindings']['has_bindable_entries'])
        platform.store.save('request', before.request['id'], before.request)
        old_job = dict(id='original-analysis', harness_id=identity, request_id=before.request['id'], status='COMPLETED',
                       submitted=dict(candidate=copy.deepcopy(candidate), report=copy.deepcopy(report)),
                       implementation=platform.analysis.freeze_implementation(before), messages=[], usage=[], model_calls=0)
        platform.store.save('analysis', old_job['id'], old_job)
        source.update(status='ANALYZED', analysis_id=old_job['id'], request_id=before.request['id'], installation_id=old_job['id'])
        platform.store.save('harness', identity, source)
        platform.dependencies.record(before, candidate)
        old_units = platform.catalog.install(before, candidate, old_job['id'])
        self.assertTrue(any(unit['status'] == 'READY' for unit in old_units))
        old_snapshot = copy.deepcopy(platform.store.get('analysis', old_job['id']))
        old_bytes = before.data('original.py')
        self.write('original.py', 'def main(value: str) -> str:\n    return "new:" + value\n')
        with patch.object(platform.analysis, 'start') as start:
            platform.profiles.role_identity = lambda role: role
            update = platform.analyze_update(identity)
        self.assertEqual(update['update_status'], 'ANALYZING')
        updated_inspector = start.call_args.args[0]
        current_report = platform.analysis._check_candidate(updated_inspector, candidate)
        self.assertTrue(current_report['entry_bindings']['has_bindable_entries'])
        job = dict(id=update['analysis_id'], revision_id=update['revision_id'], harness_id=identity,
            kind='SOURCE_UPDATE_ANALYSIS', completion_scope='candidate_and_report_only', status='COMPLETED',
            submitted=dict(candidate=copy.deepcopy(candidate), report=current_report),
            implementation=platform.analysis.freeze_implementation(updated_inspector), messages=[], usage=[], model_calls=0)
        platform.store.save('analysis', job['id'], job)
        old_id = old_units[0]['id']
        old_state = platform.store.get('unit', old_id)
        old_bindings = dict(platform.catalog.bindings)
        with patch('seampact.hosting.deployment.runtime_groups', side_effect=RuntimeError('fixture install failure')):
            units = platform._connect_units(updated_inspector, job)
        self.assertEqual(units, [])
        self.assertEqual(job['update_installation']['status'], 'NOT_INSTALLED')
        self.assertEqual(platform.store.get('harness', identity)['analysis_id'], old_job['id'])
        self.assertEqual(platform.store.get('unit', old_id), old_state)
        self.assertEqual(platform.catalog.bindings, old_bindings)
        visible = [unit for unit in platform.catalog.public() if unit['source_id'] == identity]
        self.assertEqual([unit['id'] for unit in visible], [old_id])
        self.assertEqual(platform.catalog.invoke(old_id, {'value': 'fixture'}), 'old:fixture')


    def test_update_resume_retains_staging_and_rejects_edits(self):
        self.write('original.py', 'def main(value: str) -> str:\n    return "old:" + value\n')
        platform = Platform(self.home / 'actual-platform')
        self.addCleanup(platform.store.close)
        self.addCleanup(platform.runtime.close)
        identity = platform.add_harness({'address': str(self.root)})['id']
        source = platform.store.get('harness', identity)
        before = Inspector.freeze_repository(source, platform.home / 'snapshots', 'original-request', 'original observation')
        before.request.update(rules=copy.deepcopy(RULESET), entry_profile=copy.deepcopy(NATIVE_PROFILE),
            resource_catalog=platform.resource_catalog(), dependency_bindings={})
        candidate = dict(entries=[dict(id='main', kind='function', path='original.py', symbol='main')],
                         boundaries=[], dependencies=[], runtime_groups=[dict(id='whole', entries=['main'])])
        report = platform.analysis._check_candidate(before, candidate)
        self.assertTrue(report['entry_bindings']['has_bindable_entries'])
        platform.store.save('request', before.request['id'], before.request)
        old_job = dict(id='original-analysis', harness_id=identity, request_id=before.request['id'], status='COMPLETED',
                       submitted=dict(candidate=copy.deepcopy(candidate), report=copy.deepcopy(report)),
                       implementation=platform.analysis.freeze_implementation(before), messages=[], usage=[], model_calls=0)
        platform.store.save('analysis', old_job['id'], old_job)
        source.update(status='ANALYZED', analysis_id=old_job['id'], request_id=before.request['id'], installation_id=old_job['id'])
        platform.store.save('harness', identity, source)
        platform.dependencies.record(before, candidate)
        old_units = platform.catalog.install(before, candidate, old_job['id'])
        self.assertTrue(any(unit['status'] == 'READY' for unit in old_units))
        old_snapshot = copy.deepcopy(platform.store.get('analysis', old_job['id']))
        old_bytes = before.data('original.py')
        self.write('original.py', 'def main(value: str) -> str:\n    return "new:" + value\n')
        profile = dict(provider='fixture', base_url='http://localhost', model='fixture', auth={'mode': 'none'})
        real_start = threading.Thread.start
        def start_other_threads(thread):
            if getattr(thread, '_target', None) != platform.analysis._run:
                return real_start(thread)
        with patch.object(platform.profiles, 'role_identity', return_value='analysis'), \
                patch.object(platform.profiles, 'profile', return_value=profile), \
                patch.object(platform.profiles, 'key', return_value=None), \
                patch.object(threading.Thread, 'start', start_other_threads):
            update = platform.analyze_update(identity)
            previous = platform.store.get('analysis', update['analysis_id'])
            previous.update(status='FAILED', failure_phase='model_request', error_kind='transport', rounds=1,
                messages=[previous['messages'][0], previous['messages'][1],
                          dict(role='assistant', tool_calls=[dict(id='call')]),
                          dict(role='tool', tool_call_id='call', content='ok')])
            platform.store.save('analysis', previous['id'], previous)
            draft = platform.store.get('analysis_revision', update['revision_id'])
            changed = copy.deepcopy(draft)
            changed['guidance'] = 'changed'
            platform.store.save('analysis_revision', draft['id'], changed)
            with self.assertRaisesRegex(ValueError, '草稿'):
                platform.resume_analysis(previous['id'])
            platform.store.save('analysis_revision', draft['id'], draft)
            request = platform.store.get('request', previous['request_id'])
            platform.store.save('request', previous['request_id'], dict(request, observation='changed'))
            with self.assertRaisesRegex(ValueError, '冻结请求'):
                platform.resume_analysis(previous['id'])
            platform.store.save('request', previous['request_id'], request)
            saved_source = platform.store.get('harness', identity)
            platform.store.save('harness', identity, dict(saved_source, revision='changed'))
            with self.assertRaisesRegex(ValueError, '改变'):
                platform.resume_analysis(previous['id'])
            platform.store.save('harness', identity, saved_source)
            result = platform.resume_analysis(previous['id'])
            resumed = platform.store.get('analysis', result['analysis_id'])
            self.assertEqual(resumed['kind'], 'SOURCE_UPDATE_ANALYSIS')
            self.assertEqual(resumed['completion_scope'], 'candidate_and_report_only')
            self.assertEqual(resumed['pipeline_stage'], previous['pipeline_stage'])
            self.assertEqual(resumed['request_id'], previous['request_id'])
            self.assertEqual(resumed['revision_id'], update['revision_id'])
            self.assertNotIn('repair', resumed)
            self.assertEqual(platform.store.get('harness', identity)['analysis_id'], old_job['id'])
            self.assertEqual(platform.catalog.invoke(old_units[0]['id'], {'value': 'fixture'}), 'old:fixture')
            from seampact.analysis import RULES
            resumed.update(status='FAILED', failure_phase='model_request', error_kind='transport',
                           rounds=1, pipeline_stage='analysis')
            resumed['messages'][0] = dict(role='system', content=RULES)
            platform.store.save('analysis', resumed['id'], resumed)
            second = platform.resume_analysis(resumed['id'])
            resumed_analysis = platform.store.get('analysis', second['analysis_id'])
            self.assertEqual(resumed_analysis['pipeline_stage'], 'analysis')
            self.assertEqual(resumed_analysis['revision_id'], update['revision_id'])


class SourceExecutionEvidenceChecks(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(Path(self.directory.name) / "platform.sqlite")
        self.addCleanup(self.store.close)
        self.registry = Dependencies(self.store, None, None)
        self.inspector = SimpleNamespace(request=dict(source_id="source", source_version="v1"))
        self.row = dict(id="model", definition_digest="same-original-mapping", entry_ids=["main"],
            status="MAPPING_CHECKED", selection_allowed=True)
        self.selection = dict(dependency_id="source:model", origin_id="model", source_id="source",
            definition_digest=self.row["definition_digest"], revision_digest="resource-v1", status="SELECTED")
        self.store.create("dependency_binding", "source:model", self.selection)

    def record(self):
        with patch("seampact.configuration.dependencies.check_requirements", return_value=[copy.deepcopy(self.row)]):
            self.registry.record(self.inspector, {})
            bindings = DependencyBindings(None, self.inspector, {})
        bindings.selected = {"model": copy.deepcopy(self.selection)}
        return bindings.snapshot()

    def test_execution_basis_matches_analysis_input_contract(self):
        self.inspector.request.update(resource_catalog={"weights": []}, configuration={"entries": {}},
            runtime_environment={"python": "3.11"}, dependency_bindings={"model": self.selection})
        self.assertEqual(_execution_basis(self.inspector)["input_digest"],
                         content_digest(preparation_input(self.inspector)))

    def test_same_mapping_new_source_does_not_inherit_execution_or_accept_old_result(self):
        old = self.record()
        outcome = self.registry.record_execution("source", "main", True, old)
        self.assertEqual(outcome["resources"][0]["execution_basis"]["source_version"], "v1")
        self.assertEqual(self.store.get("resource_dependency", "source:model")["execution"], "SUCCEEDED")
        self.inspector.request["source_version"] = "v2"
        self.record()
        self.registry.record_execution("source", "main", True, old)
        row = self.store.get("resource_dependency", "source:model")
        self.assertEqual(row["execution"], "NOT_RUN")
        self.assertNotIn("last_execution", row)
        selection = self.store.get("dependency_binding", "source:model")
        self.assertEqual(selection["execution"], "NOT_RUN")
        self.assertNotIn("last_execution", selection)
        self.assertEqual(self.registry.selections("source")["model"]["revision_digest"], "resource-v1")

    def test_configuration_change_does_not_accept_old_frozen_result(self):
        old = self.record()
        self.inspector.request["configuration"] = {"entries": {"main": {"fixed_arguments": {"model": "new"}}}}
        self.record()
        self.registry.record_execution("source", "main", True, old)
        self.assertEqual(self.store.get("resource_dependency", "source:model")["execution"], "NOT_RUN")

    def test_old_resource_revision_does_not_update_new_selection_success(self):
        old = self.record()
        current = self.store.get("dependency_binding", "source:model")
        current["revision_digest"] = "resource-v2"
        self.store.save("dependency_binding", "source:model", current)
        self.registry.record_execution("source", "main", True, old)
        self.assertEqual(self.store.get("dependency_binding", "source:model")["execution"], "NOT_RUN")
        self.assertEqual(self.store.get("resource_dependency", "source:model")["execution"], "NOT_RUN")


if __name__ == "__main__":
    unittest.main()
