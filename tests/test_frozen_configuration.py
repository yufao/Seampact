"""Finite original configuration values keep runtime reader/effect conditions."""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.source.dependency_closure import dependency_closure, resolve_resource_calls
from seampact.source.frozen_configuration import (
    ConfigurationUnknown, READER_FILES, frozen_bytes, plain_yaml_value, reader_identity, reader_source_check,
)
from seampact.source.inspection import Inspector


YAML = ("engine_name: spacy\nmodels:\n  -\n    lang_code: en\n    model_name: en_core_web_lg\n"
        "options:\n  multiplier: 0.4\n  names:\n  -\n  ignored:\n  - ORGANIZATION # literal comment\n"
        "  enabled: true\n  attempts: 2\n")
SOURCE = ("from pathlib import Path\nimport yaml\nimport consumer\n"
          "def read_config(name='settings.yaml'):\n"
          "    path = Path(Path(__file__).parent, 'conf', name)\n"
          "    with open(path) as stream:\n        return yaml.safe_load(stream)\n"
          "def host():\n    config = read_config()\n"
          "    return consumer.load(config['engine_name'], config['models'][0]['model_name'], config.get('absent'))\n")
MATCHED = dict(installed_source="MATCHED", runtime_tables="UNKNOWN", loaded="NOT_LOADED", target_runtime="NOT_PROVED")


class FrozenConfigurationChecks(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reader = patch("seampact.source.dependency_closure.reader_identity", return_value=copy.deepcopy(MATCHED))
        self.reader.start()
        self.addCleanup(self.reader.stop)

    def freeze(self, source=SOURCE, data=YAML):
        files = {"main.py": source, "conf/settings.yaml": data}
        for path, text in files.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="ascii")
        inspector = Inspector.freeze(self.root, self.root / "snapshots", "configuration", files, "entry", "output")
        self.inspector = inspector
        self.entry = dict(id="host", path="main.py", kind="function", symbol="host")
        self.candidate = dict(entries=[self.entry], dependencies=[])
        return inspector

    def trace(self, source=SOURCE, data=YAML):
        self.freeze(source, data)
        before = copy.deepcopy(self.candidate)
        trace = resolve_resource_calls(self.inspector, self.entry, self.candidate)
        self.assertEqual(self.candidate, before)
        return trace

    def load_call(self, trace):
        return next(call for call in trace["calls"] if call["target"] == "consumer.load")

    def test_full_block_subset_matches_reviewed_values(self):
        value = plain_yaml_value(YAML.encode("ascii"))
        self.assertEqual(value, dict(engine_name="spacy", models=[dict(lang_code="en", model_name="en_core_web_lg")],
            options=dict(multiplier=0.4, names=[None], ignored=["ORGANIZATION"], enabled=True, attempts=2)))
        self.assertEqual(plain_yaml_value(b"flags:\n- yes\n- OFF\n- null\n- -2\n"), dict(flags=[True,False,None,-2]))

    def test_compact_sequence_maps_and_flat_flow_scalars_preserve_original_order(self):
        source = (b'recognizers:\n  - name: First\n    languages:\n    - language: en\n'
                  b'      context: [credit, card, visa]\n    enabled: true\n'
                  b'  - name: Second\n    context: []\n    values: [yes, OFF, null, -2, 0.4,]\n')
        self.assertEqual(plain_yaml_value(source), dict(recognizers=[
            dict(name='First', languages=[dict(language='en', context=['credit', 'card', 'visa'])], enabled=True),
            dict(name='Second', context=[], values=[True, False, None, -2, 0.4])]))
        long = ('# source comment\n' * 600 + 'recognizers:\n  - name: Original\n').encode('ascii')
        self.assertEqual(plain_yaml_value(long), dict(recognizers=[dict(name='Original')]))
        with self.assertRaises(ConfigurationUnknown):
            plain_yaml_value(b'# bounded\n' * 2049 + b'key: value\n')

    def test_rejects_ambiguous_or_uncovered_yaml_instead_of_reinterpreting(self):
        examples = ["key: a\nkey: b\n", "yes: data\n", "a: &x data\nb: *x\n", "a: !tag data\n",
            "a: {key: data}\n", "a: [[data]]\n", "a: [data,,other]\n", 'a: "data"\n', "a: .nan\n", "a: .inf\n",
            "a: 2026-01-01\n", "a: 0123\n", "a: |\n  data\n", "a:\n\tkey: data\n",
            "---\na: data\n", "a:\n  <<: data\n", "a:\n  key: data\n key2: data\n",
            "a:\n  - key: data\n    key: other\n", "a: data\n  b: other\n", "" ]
        for text in examples:
            with self.subTest(text=text), self.assertRaises(ConfigurationUnknown):
                plain_yaml_value(text.encode("ascii"))
        for data in (b"a: " + b"x"*65536, b"a:\tdata\n", b"a: \xff\n"):
            with self.assertRaises(ConfigurationUnknown):
                plain_yaml_value(data)

    def test_code_match_is_separate_from_loaded_tables_and_target_identity(self):
        folder = self.root / "reader"
        folder.mkdir()
        for name in READER_FILES:
            (folder/name).write_bytes(b"modified reader, never executed")
        observation = reader_identity(folder)
        self.assertEqual(observation["installed_source"], "MISMATCH")
        self.assertEqual(observation["runtime_tables"], "UNKNOWN")
        self.assertEqual(observation["target_runtime"], "NOT_PROVED")

    def test_original_read_values_propagate_but_never_remove_runtime_conditions(self):
        trace = self.trace()
        call = self.load_call(trace)
        self.assertEqual([item["value"] for item in call["args"]], ["spacy", "en_core_web_lg", None])
        self.assertEqual(call["path_condition_status"], "UNKNOWN")
        self.assertEqual(trace["status"], "MISSING")
        self.assertEqual(trace["negative_scope_status"], "UNKNOWN")
        fact = trace["configuration_evidence"][0]
        self.assertEqual(fact["value_status"], "CONDITIONAL")
        self.assertEqual(fact["condition"]["reader_identity"]["runtime_tables"], "UNKNOWN")
        self.assertEqual(fact["condition"]["sha256"], self.inspector.files['conf/settings.yaml']['sha256'])
        self.assertEqual(fact["condition"]["lifecycle"], "UNKNOWN")
        self.assertEqual(fact['condition']['original_text_encoding'],dict(argument='OMITTED',resolved='UNKNOWN_ORIGINAL_RUNTIME_DEFAULT'))
        self.assertTrue(any('decoding' in text for text in fact['condition']['required']))
        explicit = self.trace(SOURCE.replace('open(path)', "open(path, encoding='utf-8')"))
        self.assertEqual(explicit['configuration_evidence'][0]['condition']['original_text_encoding'],
                         dict(argument='utf-8',resolved='utf-8'))

    def test_modified_reader_or_registered_constructor_stays_unknown(self):
        variants = [SOURCE.replace("import consumer", "import consumer\nyaml.safe_load = consumer.reader"),
                    SOURCE.replace("import consumer", "import consumer\nyaml.SafeLoader.add_constructor('!x', consumer.reader)"),
                    SOURCE.replace("import consumer", "import consumer\nfrom yaml import SafeLoader as Reader\nReader.yaml_constructors['!x'] = consumer.reader")]
        for source in variants:
            with self.subTest(source=source):
                trace = self.trace(source)
                self.assertEqual(trace["configuration_evidence"], [])
                self.assertEqual(self.load_call(trace)["args"][0]["status"], "UNKNOWN")

    def test_mismatching_installed_reader_has_no_config_value_fact(self):
        self.reader.stop()
        with patch("seampact.source.dependency_closure.reader_identity", return_value=dict(installed_source="MISMATCH")):
            trace = self.trace()
        self.assertEqual(trace["configuration_evidence"], [])
        self.assertTrue(any(row['reason']=='frozen_configuration_reader_or_stream_unproved' for row in trace['unresolved_targets']))

    def test_unproven_read_mode_path_or_stream_has_no_value_fact(self):
        variants = [SOURCE.replace("open(path)", "open(path, 'rb')"),
                    SOURCE.replace("open(path)", "open(path, mode={})"),
                    SOURCE.replace("open(path)", "open(path, encoding=[])"),
                    SOURCE.replace("open(path)", "open('conf/settings.yaml')"),
                    SOURCE.replace("return yaml.safe_load(stream)", "stream.read()\n        return yaml.safe_load(stream)"),
                    SOURCE.replace("return yaml.safe_load(stream)", "consumer.escape(stream)\n        return yaml.safe_load(stream)"),
                    SOURCE.replace("return yaml.safe_load(stream)", "reader = stream.read\n        reader()\n        return yaml.safe_load(stream)"),
                    SOURCE.replace("return yaml.safe_load(stream)", "consumer.escape([stream])\n        return yaml.safe_load(stream)"),
                    SOURCE.replace("return yaml.safe_load(stream)", "box = {'file': stream}\n        consumer.escape(box)\n        return yaml.safe_load(stream)"),
                    SOURCE.replace("return yaml.safe_load(stream)", "for line in stream:\n            pass\n        return yaml.safe_load(stream)"),
                    SOURCE.replace("return yaml.safe_load(stream)", "a, b = stream\n        return yaml.safe_load(stream)"),
                    SOURCE.replace("return yaml.safe_load(stream)", "a = [*stream]\n        return yaml.safe_load(stream)"),
                    SOURCE.replace("return yaml.safe_load(stream)", "consumer.escape(*stream)\n        return yaml.safe_load(stream)"),
                    SOURCE.replace("return yaml.safe_load(stream)", "a = [line for line in stream]\n        return yaml.safe_load(stream)"),
                    SOURCE.replace("return yaml.safe_load(stream)", "stream.read = lambda *args: 'other: value'\n        return yaml.safe_load(stream)"),
                    SOURCE.replace("return yaml.safe_load(stream)", "del stream.read\n        return yaml.safe_load(stream)"),
                    SOURCE.replace("return yaml.safe_load(stream)", "box = [stream]\n        box[0].read = consumer.reader\n        return yaml.safe_load(stream)"),
                    SOURCE.replace("return yaml.safe_load(stream)", "if 'x' in stream:\n            pass\n        return yaml.safe_load(stream)"),
                    SOURCE.replace("return yaml.safe_load(stream)", "consumer.escape(locals())\n        return yaml.safe_load(stream)"),
                    SOURCE.replace("return yaml.safe_load(stream)", "pass\n    return yaml.safe_load(stream)"),
                    SOURCE.replace("return yaml.safe_load(stream)", "return yaml.safe_load(unknown)")]
        for source in variants:
            with self.subTest(source=source):
                self.assertEqual(self.trace(source)["configuration_evidence"], [])

    def test_source_reader_alias_and_dynamic_escapes_are_unknown_finite_forms(self):
        changes = ["a, b = yaml.SafeLoader, 1", "a = [yaml.SafeLoader]", "a = {'loader': yaml.SafeLoader}",
                   "a = getattr(yaml, 'SafeLoader')", "setattr(yaml, 'SafeLoader', consumer.reader)",
                   "def expose():\n    return yaml.SafeLoader", "def expose():\n    return lambda: yaml.SafeLoader"]
        for change in changes:
            with self.subTest(change=change):
                self.freeze(SOURCE+'\n'+change+'\n')
                facts = reader_source_check(self.inspector)
                self.assertEqual(facts['status'], 'UNKNOWN')
                self.assertEqual(facts['runtime_state'], 'UNKNOWN')
                self.assertNotIn('unmodified', facts)
        self.freeze()
        facts = reader_source_check(self.inspector)
        self.assertEqual(facts['status'], 'FINITE_FORMS_CHECKED')
        self.assertEqual(facts['runtime_state'], 'UNKNOWN')

    def test_conditional_values_cannot_authorize_fixed_loader_not_used_or_return_shape(self):
        from seampact.configuration.resource_call_bindings import _bound_call, resource_scope
        from seampact.source.result_structure import derive_result_structure
        source = SOURCE.replace('import consumer', 'import consumer\nfrom transformers import AutoModel')
        source = source.replace("    return consumer.load(config['engine_name'], config['models'][0]['model_name'], config.get('absent'))",
            "    if config['engine_name'] == 'spacy':\n        return 1\n    return AutoModel.from_pretrained('owner/other')")
        self.freeze(source)
        row = dict(id='weights',kind='weight',configurable=False,entry_ids=['host'],mappings=[],
            placement=dict(expression="'owner/other'"), evidence=[dict(path='main.py',start=1,end=len(source.splitlines()))])
        scope = resource_scope(self.inspector,self.entry,row,self.candidate)
        self.assertEqual(scope['status'],'UNKNOWN')
        shape = derive_result_structure(self.inspector,self.entry,self.candidate)
        self.assertEqual(shape['status'],'UNKNOWN')
        self.assertIsNone(shape['result_schema'])
        trace = self.trace()
        with self.assertRaisesRegex(ValueError, 'path/extra argument domain'):
            _bound_call(self.load_call(trace), {})

    def test_repeated_read_and_changed_path_factory_remain_unknown(self):
        variants = [SOURCE.replace("return yaml.safe_load(stream)", "yaml.safe_load(stream)\n        return yaml.safe_load(stream)"),
                    SOURCE.replace("import consumer", "import consumer\nimport pathlib\npathlib.Path = consumer.factory"),
                    SOURCE.replace("import consumer", "import consumer\nimport builtins\nbuiltins.open = consumer.factory")]
        for source in variants:
            with self.subTest(source=source):
                trace = self.trace(source)
                self.assertEqual(self.load_call(trace)['args'][0]['status'], 'UNKNOWN')

    def test_source_and_custom_yaml_hooks_are_never_executed(self):
        marker = self.root/'source-executed'
        source = SOURCE + "\ndef arbitrary_hook(loader, node):\n    raise AssertionError('never execute source hook')\n"
        source += "\nraise AssertionError('never execute source module')\n"
        trace = self.trace(source)
        self.assertTrue(trace['configuration_evidence'])
        self.assertFalse(marker.exists())

    def test_other_open_protocols_keep_their_original_unknown_effect_scope(self):
        source = ("class Name:\n    def __fspath__(self):\n        return consumer.effect()\n"
                  "import consumer\ndef host():\n    return open(Name())\n")
        trace = self.trace(source)
        self.assertTrue(any('implicit owner protocol' in gap['reason'] for gap in trace['negative_scope_gaps']))

    def test_content_drift_is_rechecked_instead_of_using_a_cached_value(self):
        self.freeze()
        self.assertTrue(dependency_closure(self.inspector, self.candidate)['entries']['host']['configuration_conditions'])
        record = self.inspector.files['conf/settings.yaml']
        (self.inspector.snapshots/record['sha256']).write_bytes(b"engine_name: another\n")
        row = dependency_closure(self.inspector, self.candidate)['entries']['host']
        self.assertFalse(row['configuration_conditions'])
        self.assertTrue(any(i['reason'].startswith('frozen_configuration_') for i in row['unresolved_targets']))

    def test_absolute_escape_and_symlink_paths_cannot_select_frozen_content(self):
        self.freeze()
        for path in ('/conf/settings.yaml', '../conf/settings.yaml', 'conf/../../settings.yaml'):
            with self.subTest(path=path), self.assertRaises(ConfigurationUnknown):
                frozen_bytes(self.inspector, path)
        link = self.root/'alias'
        try:
            link.symlink_to(self.root/'conf', target_is_directory=True)
        except OSError:
            return  # Windows symlink privilege is independent of source rules.
        with self.assertRaises(ConfigurationUnknown):
            frozen_bytes(self.inspector, 'alias/../conf/settings.yaml')

    def test_get_keeps_original_default_evaluation_and_unknown_mutation(self):
        source = SOURCE.replace("    return consumer.load(config['engine_name'], config['models'][0]['model_name'], config.get('absent'))",
            "    value = config.get('engine_name', consumer.side_effect())\n    consumer.escape(config)\n    return consumer.load(config.get('absent'))")
        trace = self.trace(source)
        calls = [call['target'] for call in trace['calls']]
        self.assertLess(calls.index('consumer.side_effect'), calls.index('consumer.escape'))
        self.assertEqual(self.load_call(trace)['args'][0]['status'], 'UNKNOWN')

    def test_configuration_does_not_enable_general_short_circuit_or_comprehensions(self):
        source = SOURCE.replace("    return consumer.load(config['engine_name'], config['models'][0]['model_name'], config.get('absent'))",
            "    choices = {key: consumer for key in config}\n    if config.get('absent') and config['engine_name']:\n        eval('not closed')\n    return choices['engine_name']()")
        trace = self.trace(source)
        self.assertTrue(any(row['target']=='eval' for row in trace['unresolved_targets']))
        self.assertTrue(any(row['reason']=='dynamic_call_target_unresolved' for row in trace['unresolved_targets']))


if __name__ == '__main__':
    unittest.main()
