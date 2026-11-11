"""Normal Core start/attach frames bind births; no source or Docker is executed."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from seampact.hosting import backends
from seampact.hosting.deployment import Deployments, GROUP_PROFILE, IDENTITY_PROTOCOL, binding_fingerprint
from seampact.hosting.environments import normalize_environment
from seampact.hosting.image_build import worker_dockerfile
from seampact.hosting.value_conditioned_birth import (before_start, confirm_start, bind_registration, read_birth)
from seampact.hosting import value_conditioned_birth as birth_module
from seampact.service import Platform
from seampact.source.inspection import Inspector
from seampact.target_runtime import core_identity


IMAGE = 'sha256:' + 'a' * 64
BASE = 'sha256:' + 'b' * 64


class ValueConditionedBirthTests(unittest.TestCase):
    def fixture(self, *, pass_env=(), mount='/models'):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        platform = Platform(root / 'platform')
        self.addCleanup(platform.close)
        source = root / 'bundle' / 'source'
        source.mkdir(parents=True)
        (source / 'original.py').write_text(
            'def unknown(value):\n return eval(value)\n'
            'def score(value: str) -> int:\n if value == "safe":\n  return 7\n return unknown(value)\n', encoding='utf-8')
        inspector = Inspector.freeze(source, root / 'bundle' / 'snapshots', 'request', ['original.py'], 'score', 'result')
        inspector.request['source_id'] = 'source'
        entry = dict(id='score', path='original.py', symbol='score', kind='function', boundary_ids=['whole'])
        candidate = dict(entries=[entry], boundaries=[dict(id='whole', path='original.py', symbol='score', mode='W')],
                         runtime_groups=[])
        unit, = platform.catalog.install(inspector, candidate, 'analysis', 'installation')
        self.assertEqual(unit['status'], 'STRUCTURE_ONLY', unit.get('reason'))
        binding, entry = platform.catalog.structural[unit['id']]
        group = platform.store.get('runtime_group', unit['runtime_group'])
        self.assertEqual(group['contract']['operations']['start']['status'], 'UNKNOWN')
        platform.store.save('harness', 'source', dict(id='source', status='CONNECTED', installation_id='installation'))
        environment = normalize_environment({}, inspector)
        folder = root / 'bundle'
        spec = dict(schema='native-worker/1', profile=GROUP_PROFILE['id'], contract_digest=group['contract']['digest'],
                    group=group, request=binding.inspector.request, candidate=binding.candidate,
                    runtime_environment={'python': '3.11'}, platform_core=core_identity('worker'),
                    analysis_id='analysis', installation_id='installation')
        (folder / 'worker.json').write_text(json.dumps(spec), encoding='utf-8')
        check_python = ("import sys; from seampact.hosting.environments import check_python_version; "
                        "assert '.'.join(map(str,sys.version_info[:2])) == '3.11'; "
                        "r=check_python_version('.'.join(map(str,sys.version_info[:3])), []); "
                        "assert r['status'] == 'SUPPORTED', r['missing']")
        reuse = worker_dockerfile(folder, inspector, environment, check_python)
        artifact = dict(id=group['id'], status='ARTIFACT_READY', folder=str(folder), image='fixture-worker:one',
                        profile=GROUP_PROFILE['id'], contract_digest=group['contract']['digest'],
                        binding_fingerprint=binding_fingerprint(spec), environment=environment, build_reuse=reuse,
                        platform_core=spec['platform_core'],
                        build=dict(status='BUILT', builder_driver='docker', requested_base_image='python:3.11-slim',
                                   image_id=IMAGE, resolved_base_id=BASE, steps=[dict(returncode=0, argv=[
                                       'docker', 'buildx', 'build', '--load', '--builder', 'default', '-t', 'fixture-worker:one',
                                       '--build-arg', 'BASE_IMAGE=' + backends.content_image_reference(BASE), str(folder)])]),
                        publish=dict(status='NOT_REQUIRED', reference=IMAGE))
        platform.store.save('deployment', group['id'], artifact)
        config = backends.validate_config(dict(kind='docker', coordinator_url='http://coordinator.invalid',
                                              docker_network='fixture-network', gpus='', pass_env=list(pass_env),
                                              mounts=[dict(source='/host/models', target=mount, read_only=True)]))
        platform.store.save('deployment_backend', 'current', config)
        return platform, unit, group, binding, entry, spec

    def start(self, fixture, *, change_command=False):
        platform, unit, group, binding, entry, spec = fixture
        owner, captured, runs = platform.deployments, {}, []

        def capture_before(*args):
            token = before_start(*args)
            record = args[1]
            captured['pending'] = read_birth(owner, dict(session=owner.session, instance_id=record['id'], generation=record['generation']))
            return token

        def capture_confirm(*args):
            result = confirm_start(*args)
            captured['confirmed'] = result
            return result

        original_group = Deployments._group

        def mock_existing_start_precondition(instance, identity):
            # Only this external precondition is mocked to exercise the birth
            # leaf. Installed contracts retain start UNKNOWN; no source or
            # real process is started and no invocation permission is claimed.
            value = copy.deepcopy(original_group(instance, identity))
            value['contract']['operations']['start'] = dict(status='SUPPORTED', basis='mock independent start precondition')
            return value

        def run(argv, timeout, **kwargs):
            runs.append(copy.deepcopy(argv))
            reported = list(argv)
            if change_command:
                reported.insert(2, '--privileged')
            return dict(argv=reported, returncode=0, duration_ms=1, stdout='c' * 64 + '\n', stderr='')

        with patch.object(Deployments, '_start_gap', return_value=None), \
             patch.object(Deployments, '_group', mock_existing_start_precondition), \
             patch.object(Deployments, 'gpu_allocation', return_value=dict(devices=[], status='VERIFIED')), \
             patch.object(Deployments, '_binding', return_value=(binding, entry, unit)), \
             patch.object(birth_module, 'before_start', capture_before), \
             patch.object(birth_module, 'confirm_start', capture_confirm), \
             patch.object(backends, 'docker_available', return_value=None), patch.object(backends, 'run', side_effect=run):
            result = owner._start_unprotected(group['id'], None, False)
        return owner, result, captured, runs, spec

    def test_original_start_command_and_registration_bind_an_internal_birth(self):
        fixture = self.fixture()
        owner, result, captured, runs, spec = self.start(fixture)
        self.assertEqual(len(runs), 1)
        self.assertEqual(captured['pending']['status'], 'PENDING')
        self.assertEqual(captured['confirmed']['status'], 'BACKEND_CONFIRMED')
        unit, group = fixture[1:3]
        health = dict(identity_protocol=IDENTITY_PROTOCOL, profile=GROUP_PROFILE['id'], session=owner.session,
                      instance_id=result['id'], generation=result['generation'], process_nonce='actual-fixture-nonce',
                      group_id=group['id'], source_version=unit['source_version'], installation_id='installation',
                      binding_fingerprint=binding_fingerprint(spec), contract_digest=spec['contract_digest'],
                      units=[], guarded_units=[unit['id']], initialization={})
        endpoint = 'http://seampact-' + result['name'] + ':8890'
        with patch('seampact.hosting.deployment.json_request', return_value=health), \
             patch.object(Deployments, '_binding', return_value=(fixture[3], fixture[4], unit)):
            owner.attach(endpoint)
        identity = {key: health[key] for key in ('session', 'instance_id', 'generation', 'process_nonce')}
        answer = read_birth(owner, identity)
        self.assertEqual(answer['status'], 'MANAGED_BIRTH_CHECKED', answer)
        self.assertEqual(answer['handle']['container_id'], 'c' * 64)
        self.assertEqual(answer['support']['structure_digest'], unit['structure_admission']['plan_digest'])
        self.assertTrue(all(value is False for value in answer['permissions'].values()))
        self.assertEqual(fixture[3].modules, {})
        answer['support']['baseline']['python'] = 'forged'
        self.assertEqual(read_birth(owner, identity)['support']['baseline']['python'], '3.11')
        self.assertEqual(read_birth(owner, dict(identity, generation=99))['status'], 'UNKNOWN')

    def test_posthoc_origin_flags_and_serialized_token_do_not_establish_birth(self):
        platform, unit, group, _, _, _ = self.fixture()
        owner = platform.deployments
        record = dict(id='f' * 32, group_id=group['id'], session=owner.session, generation=1,
                      origin='PLATFORM_STARTED', backend='docker', profile=GROUP_PROFILE['id'], state='STARTING')
        owner.instances[record['id']] = record
        with self.assertRaisesRegex(ValueError, 'posthoc'):
            before_start(owner, record, {}, {}, IMAGE)
        self.assertEqual(read_birth(owner, dict(session=owner.session, instance_id=record['id'], generation=1))['status'], 'UNKNOWN')
        with self.assertRaises(ValueError):
            confirm_start(owner, {'approved': True}, {'status': 'APPLIED'})

    def test_custom_startup_environment_and_core_mount_fail_before_backend(self):
        for options in (dict(pass_env=['PYTHONPATH']), dict(mount='/app')):
            with self.subTest(options=options):
                with self.assertRaises(ValueError):
                    self.start(self.fixture(**options))

    def test_changed_backend_command_is_not_a_normal_birth(self):
        with self.assertRaisesRegex(ValueError, 'original managed command'):
            self.start(self.fixture(), change_command=True)


if __name__ == '__main__':
    unittest.main()
