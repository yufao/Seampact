"""Fixed original metadata is a conditional source fact, never cold permission."""
import ast
import hashlib
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.call_value_conditions import Context, digest
from seampact.source.dependency_closure import _Instance, _Class, _configuration_value
from seampact.source.sdk_transformer_protocols import (
    RULE, GAP, SDKValue, SDKProtocolUnknown, compile_fixed_transformer_profile,
    source_owner_escape, summarize_factory, sdk_attribute, verify_transformer_targets,
    sdk_call, sdk_subscript, SDKMethod, profiles_from_fixed_resources,
)


def fixture():
    content = {
        'config.json': dict(model_type='t5', architectures=['T5ForConditionalGeneration'],
            is_encoder_decoder=True, torch_dtype='float32', vocab_size=32128),
        'tokenizer_config.json': dict(tokenizer_class='T5Tokenizer', eos_token='</s>',
            added_tokens_decoder={'1': dict(content='</s>', lstrip=False, normalized=False,
                rstrip=False, single_word=False, special=True)}),
        'special_tokens_map.json': dict(eos_token='</s>'),
    }
    files = {name: json.dumps(value).encode() for name, value in content.items()}
    resource = dict(id='source:weights', origin_id='weights', kind='weight', configurable=False,
        definition_digest='original-definition', execution_basis=dict(source_version='frozen-source'),
        placement=dict(hub_cache=dict(schema='fixed-hub-cache/1', repo_id='example/original-t5',
            revision='frozen-revision', cache_dir='/example/cache', required_files=[
                dict(path=name, bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest()) for name, raw in files.items()])))
    return resource, files


class TransformerProtocolTests(unittest.TestCase):
    def test_batch_shape_transfer_and_dimension_read_do_not_invent_tokens(self):
        from seampact.source.sdk_bounded_values import BoundedInteger
        resource, files = fixture()
        profile = compile_fixed_transformer_profile(resource, files)
        checker = self.checker(profile, compiling=True)
        node = ast.parse('original(texts)').body[0].value
        tokenizer = SDKValue('tokenizer', ('original',), profile)
        batch = sdk_call(checker, tokenizer, [['evidence', 'claim']], dict(max_length=2048,
            padding=True, truncation=True, return_tensors='pt'), 'source.py', None, node)
        tokens = sdk_subscript(checker, batch, 'input_ids', 'source.py', node)
        self.assertEqual(tokens.dimensions, (2, BoundedInteger(0, 2048)))
        self.assertEqual(tokens.dtype, 'int64')
        self.assertNotIn('token_values', tokens.fields)
        size = sdk_call(checker, SDKMethod(tokens, 'size'), [0], {}, 'source.py', None, node)
        self.assertEqual(size, 2)
        moved = sdk_call(checker, SDKMethod(tokens, 'to'), [SDKValue('device', ('original-model',))], {},
                         'source.py', None, node)
        self.assertEqual(moved.dimensions, tokens.dimensions)
        self.assertFalse(verify_transformer_targets(
            [row['target_requirements'] for row in checker.protocols], profiles=(profile,))['allowed'])

    def test_ambiguous_squeeze_and_nonordinary_batch_remain_unknown(self):
        from seampact.source.sdk_bounded_values import BoundedInteger
        checker = self.checker(compiling=True)
        node = ast.parse('original(value)').body[0].value
        tensor = SDKValue('tensor', ('original',), dimensions=(BoundedInteger(1, 2), 4), dtype='float32')
        self.assertIsNone(sdk_call(checker, SDKMethod(tensor, 'squeeze'), [0], {}, 'source.py', None, node))
        hits = []
        class Unknown(list):
            def __iter__(self): hits.append('iter'); return super().__iter__()
        self.assertIsNone(sdk_call(checker, SDKValue('tokenizer', ('original',)), [Unknown(['text'])],
            dict(max_length=2048, padding=True, truncation=True, return_tensors='pt'), 'source.py', None, node))
        self.assertEqual(hits, [])

    def test_metadata_reads_normal_fixed_json_without_reading_model_weights(self):
        import tempfile
        from seampact.configuration.dependencies import DependencyBindings
        resource, files = fixture()
        with tempfile.TemporaryDirectory() as temporary:
            cache = resource['placement']['hub_cache']
            cache.update(cache_dir=temporary, revision='a' * 40, ref='main')
            snapshot = Path(temporary) / 'models--example--original-t5' / 'snapshots' / cache['revision']
            snapshot.mkdir(parents=True)
            for name, raw in files.items():
                (snapshot / name).write_bytes(raw)
            cache['required_files'].append(dict(path='pytorch_model.bin', bytes=3000000000, sha256='b' * 64))
            resource['entry_checks'] = {'score': {'status': 'MAPPING_CHECKED'}}
            binding = object.__new__(DependencyBindings)
            binding.rows = {'weights': resource}
            profiles = profiles_from_fixed_resources(binding, 'score')
            self.assertEqual(len(profiles), 1)
            self.assertFalse(profiles[0].facts()['execution_permission'])
            self.assertFalse((snapshot / 'pytorch_model.bin').exists())
            (snapshot / 'config.json').write_bytes(b'{}')
            with self.assertRaises(SDKProtocolUnknown):
                profiles_from_fixed_resources(binding, 'score')

    def checker(self, profile=None, *, compiling=False):
        checker = SimpleNamespace(inspector=SimpleNamespace(request={'source_version': 'frozen-source'},
            files={'source.py': {'sha256': 'source-file'}}),
            call_context=Context(compiling=compiling, sdk_profiles=() if profile is None else (profile,)),
            protocols=[], gaps=[])
        checker.protocol = lambda rule, path, node, **facts: checker.protocols.append(dict(rule=rule, **facts))
        checker.negative_gap = lambda path, node, reason: checker.gaps.append(reason)
        return checker

    def test_normal_fixed_metadata_bytes_and_defaults_remain_conditional(self):
        resource, files = fixture()
        before = json.dumps(resource, sort_keys=True)
        profile = compile_fixed_transformer_profile(resource, files)
        self.assertEqual(json.dumps(resource, sort_keys=True), before)
        facts = profile.facts()
        self.assertEqual(facts['tokenizer_class'], 'T5TokenizerFast')
        self.assertEqual(facts['eos_text'], '</s>')
        self.assertEqual(facts['selected_binding_count'], 0)
        self.assertFalse(facts['execution_permission'])
        self.assertTrue(facts['default_requirements'])
        facts['placement']['repo_id'] = 'changed'
        self.assertEqual(profile.facts()['placement']['repo_id'], 'example/original-t5')

    def test_wrong_bytes_remote_class_dtype_or_missing_basis_never_get_profile(self):
        resource, files = fixture()
        for changed in ('bytes', 'remote', 'dtype', 'basis'):
            source = json.loads(json.dumps(resource))
            data = dict(files)
            if changed == 'bytes': data['config.json'] += b' '
            elif changed == 'basis': source.pop('execution_basis')
            else:
                config = json.loads(data['config.json'])
                config['auto_map' if changed == 'remote' else 'torch_dtype'] = {'custom': 'remote.Code'} if changed == 'remote' else 'float16'
                data['config.json'] = json.dumps(config).encode()
                record = source['placement']['hub_cache']['required_files'][0]
                record.update(bytes=len(data['config.json']), sha256=hashlib.sha256(data['config.json']).hexdigest())
            with self.subTest(changed=changed), self.assertRaises(SDKProtocolUnknown):
                compile_fixed_transformer_profile(source, data)

    def test_original_factory_eos_carries_actual_profile_and_preserves_target_gap(self):
        profile = compile_fixed_transformer_profile(*fixture())
        checker = self.checker(profile)
        node = ast.parse("AutoTokenizer.from_pretrained('example/original-t5', cache_dir='/example/cache')").body[0].value
        value = summarize_factory(checker, 'transformers.AutoTokenizer.from_pretrained',
            ['example/original-t5'], {'cache_dir': '/example/cache'}, 'source.py', node)
        self.assertIs(type(value), SDKValue)
        self.assertEqual(value.kind, 'tokenizer')
        self.assertEqual(sdk_attribute(checker, value, 'eos_token', 'source.py', node), '</s>')
        requirements = [row['target_requirements'] for row in checker.protocols]
        self.assertTrue(all(row['input_status'] == 'CHECKED' for row in requirements))
        self.assertTrue(all(reason == GAP for reason in checker.gaps))
        target = verify_transformer_targets(requirements, profiles=(profile,))
        self.assertEqual(target['status'], 'UNKNOWN')
        self.assertFalse(target['allowed'])
        self.assertTrue(all(row['resource_metadata'] == 'MATCHED_NORMAL_FIXED_DECLARATION_AND_BYTES' for row in target['targets']))
        with self.assertRaises(SDKProtocolUnknown): bool(value)
        with self.assertRaises(SDKProtocolUnknown): iter(value)

    def test_wrong_source_or_original_cache_binding_does_not_invent_eos(self):
        profile = compile_fixed_transformer_profile(*fixture())
        for source, cache in (('different-source', '/example/cache'), ('frozen-source', '/other/cache')):
            checker = self.checker(profile)
            checker.inspector.request['source_version'] = source
            node = ast.parse('load()').body[0].value
            value = summarize_factory(checker, 'transformers.AutoTokenizer.from_pretrained',
                ['example/original-t5'], {'cache_dir': cache}, 'source.py', node)
            eos = sdk_attribute(checker, value, 'eos_token', 'source.py', node)
            self.assertIs(type(eos), SDKValue)
            self.assertNotEqual(eos, '</s>')
            self.assertEqual(checker.protocols[-1]['target_requirements']['input_status'], 'UNKNOWN')

    def test_source_owner_and_callback_carriers_are_not_ordinary_factory_arguments(self):
        cls = _Class('source.py', 'Owner', ast.parse('class Owner: pass').body[0], {}, [], {})
        owner = _Instance('Owner()', {}, cls)
        self.assertEqual(source_owner_escape(_configuration_value({'text': ['value', None]})), 'NO_SOURCE_OWNER_ARGUMENT')
        for value in (owner, {'nested': [owner]}, lambda: None, None):
            self.assertEqual(source_owner_escape(value), 'UNKNOWN')
        checker = self.checker()
        self.assertIsNone(summarize_factory(checker, 'transformers.AutoTokenizer.from_pretrained', [owner], {},
                                            'source.py', ast.parse('load()').body[0].value))

    def test_compilation_exports_parameterized_obligation_without_actual_permission(self):
        checker = self.checker(compiling=True)
        node = ast.parse("AutoTokenizer.from_pretrained('example/original-t5', cache_dir='/example/cache')").body[0].value
        value = summarize_factory(checker, 'transformers.AutoTokenizer.from_pretrained',
            ['example/original-t5'], {'cache_dir': '/example/cache'}, 'source.py', node)
        requirement = checker.protocols[-1]['target_requirements']
        self.assertEqual(requirement['input_status'], 'REQUIRES_ACTUAL_CALL')
        self.assertEqual(checker.gaps, [])
        self.assertIs(type(value), SDKValue)
        self.assertFalse(verify_transformer_targets([requirement])['allowed'])


if __name__ == '__main__':
    unittest.main()
