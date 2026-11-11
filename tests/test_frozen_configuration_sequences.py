"""The normal frozen-reader path visits all original finite configuration rows."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.call_value_conditions import call_context, contextual
from seampact.source.dependency_closure import resolve_resource_calls, _Closure, _configuration_value
from seampact.source.frozen_configuration_sequences import (
    RULE, configuration_items, configuration_plain, configuration_method, UNHANDLED,
)
import test_frozen_configuration as fixtures


SOURCE = '''from pathlib import Path
import yaml
import consumer
def read_config(name='settings.yaml'):
    path = Path(Path(__file__).parent, 'conf', name)
    with open(path) as stream:
        return yaml.safe_load(stream)
def host():
    config = read_config()
    names = []
    for record in config['recognizers']:
        consumer.validate(record['name'], record['enabled'])
        if not record['enabled']:
            continue
        names.append(record['name'])
    else:
        consumer.finished()
    return consumer.result(names)
'''


def data(count=95):
    return 'recognizers:\n' + ''.join(
        '  - name: Entry' + str(index) + '\n    enabled: ' + ('false' if index in (15, 71) else 'true') + '\n'
        for index in range(count))


class FrozenConfigurationSequenceTests(unittest.TestCase):
    def fixture(self, source=SOURCE, content=None):
        case = fixtures.FrozenConfigurationChecks('test_original_read_values_propagate_but_never_remove_runtime_conditions')
        case.setUp()
        self.addCleanup(case.doCleanups)
        inspector = case.freeze(source, content or data())
        shadow = contextual(inspector, call_context({})[0])
        return shadow, case.entry, case.candidate

    def trace(self, source=SOURCE, content=None):
        inspector, entry, candidate = self.fixture(source, content)
        return resolve_resource_calls(inspector, entry, candidate)

    def test_normal_reader_visits_all95_in_original_order_including_disabled(self):
        trace = self.trace()
        calls = [row for row in trace['calls'] if row['target'] == 'consumer.validate']
        self.assertEqual(len(calls), 95, trace['negative_scope_gaps'])
        self.assertEqual([row['args'][0]['value'] for row in calls], ['Entry' + str(index) for index in range(95)])
        self.assertEqual([row['args'][1]['value'] for row in calls].count(False), 2)
        result = next(row for row in trace['calls'] if row['target'] == 'consumer.result')
        self.assertEqual(len(result['args'][0]['value']), 93)
        self.assertEqual(len([row for row in trace['calls'] if row['target'] == 'consumer.finished']), 1)
        self.assertTrue(any(row['rule'] == RULE and row.get('count') == 95 for row in trace['external_protocols']))
        self.assertNotEqual(trace['status'], 'CHECKED')
        self.assertTrue(trace['configuration_evidence'])
        self.assertTrue(all(row['path_condition_status'] == 'UNKNOWN' for row in calls))

    def test_break_return_and_for_else_follow_original_control(self):
        source = SOURCE.replace("if not record['enabled']:\n            continue", "if record['name'] == 'Entry4':\n            break")
        trace = self.trace(source)
        self.assertEqual(len([row for row in trace['calls'] if row['target'] == 'consumer.validate']), 5)
        self.assertFalse(any(row['target'] == 'consumer.finished' for row in trace['calls']))
        source = SOURCE.replace("if not record['enabled']:\n            continue", "if record['name'] == 'Entry3':\n            return consumer.early(record['name'])")
        trace = self.trace(source)
        self.assertEqual(len([row for row in trace['calls'] if row['target'] == 'consumer.validate']), 4)
        self.assertTrue(any(row['target'] == 'consumer.early' for row in trace['calls']))
        self.assertFalse(any(row['target'] in {'consumer.finished', 'consumer.result'} for row in trace['calls']))

    def test_unknown_alias_writer_and_unknown_branch_never_keep95_proof(self):
        sources = [SOURCE.replace('names = []', "consumer.mutate(config['recognizers'])\n    names = []"),
                   SOURCE.replace('names = []', "consumer.mutate(config['recognizers'][3])\n    names = []"),
                   SOURCE.replace("if not record['enabled']:", 'if consumer.unknown():')]
        for source in sources:
            with self.subTest(source=source):
                trace = self.trace(source)
                self.assertFalse(any(row['rule'] == RULE and row.get('count') == 95 for row in trace['external_protocols']))
                self.assertEqual(trace['negative_scope_status'], 'UNKNOWN')

    def test_reader_provenance_does_not_raise_ordinary_group_port64_limit(self):
        inspector, entry, candidate = self.fixture()
        checker = _Closure(inspector, candidate)
        detached = _configuration_value({'recognizers': [{'name': str(index)} for index in range(95)]})['recognizers']
        self.assertFalse(checker.call_plain(detached))
        self.assertIsNone(configuration_items(checker, detached))
        self.assertFalse(configuration_plain(checker, detached))

    def test_checked_copy_preserves_aliases_unknown_mutation_cannot_requalify(self):
        inspector, entry, candidate = self.fixture()
        checker = _Closure(inspector, candidate)
        checker.entry(entry)
        ledger = checker.frozen_configuration_sequences
        original = next(row['value'] for row in ledger.values() if type(row['value']) is dict and 'recognizers' in row['value'])
        node = __import__('ast').parse('value.copy()').body[0].value
        cloned = configuration_method(checker, original, 'copy', [], {}, 'main.py', node)
        self.assertIs(cloned['recognizers'], original['recognizers'])
        self.assertEqual(len(configuration_items(checker, cloned['recognizers'])), 95)
        original['recognizers'][0]['name'] = 'unproved mutation'
        self.assertIsNone(configuration_items(checker, cloned['recognizers']))
        self.assertIs(configuration_method(checker, cloned, 'copy', [], {}, 'main.py', node), UNHANDLED)

    def test_original_frozen_dict_comprehension_merge_preserves_all95_aliases(self):
        source = SOURCE.replace('names = []', "provided = {'language': 'en'}\n    provided.update({key: value for key, value in config.items() if key not in list(provided.keys())})\n    config = provided\n    names = []")
        trace = self.trace(source)
        calls = [row for row in trace['calls'] if row['target'] == 'consumer.validate']
        self.assertEqual(len(calls), 95)
        self.assertTrue(any(row['rule'] == RULE and row.get('operation') == 'original exact ordered comprehension'
                            for row in trace['external_protocols']))

    def test_plain_dict_copy_and_view_keep_shallow_aliases_without_reader_origin(self):
        inspector, entry, candidate = self.fixture()
        checker = _Closure(inspector, candidate)
        original = {'languages': ['en']}
        node = __import__('ast').parse('value.copy()').body[0].value
        result = configuration_method(checker, original, 'copy', [], {}, 'main.py', node)
        self.assertIsNot(result, original)
        self.assertIs(result['languages'], original['languages'])
        self.assertNotIn(id(result), checker.frozen_configuration_sequences)
        view = configuration_method(checker, original, 'keys', [], {}, 'main.py', node)
        self.assertEqual(configuration_items(checker, view), ('languages',))
        original['added'] = 'value'
        self.assertIsNone(configuration_items(checker, view))


if __name__ == '__main__':
    unittest.main()
