"""Source class enumeration never substitutes an invented native set order."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.call_value_conditions import call_context, contextual, compile_condition
from seampact.source.dependency_closure import resolve_resource_calls
import test_activation_prefix as fixture


SOURCE = '''class Root:
    pass
class Left(Root):
    pass
class Right(Root):
    pass
class Leaf(Left):
    pass
class Selector:
    @staticmethod
    def members(cls=None):
        if not cls:
            cls = Root
        return set(cls.__subclasses__()).union(
            [s for c in cls.__subclasses__() for s in Selector.members(c)])
    @staticmethod
    def choose(name):
        members = Selector.members()
        for member in members:
            if name == member.__name__:
                return member
        return None
def score(value):
    selected = Selector.choose(value)
    if selected is not None:
        return selected.__name__
    return 'missing'
'''


class FiniteRegistrationClosureTests(unittest.TestCase):
    def trace(self, source=SOURCE, value='Leaf', other_files=None):
        check = fixture.ActivationPrefixTests('test_parent_package_runs_before_child_and_is_not_repeated')
        self.addCleanup(check.doCleanups)
        inspector, entry, candidate = check.fixture(dict({'original.py': source}, **(other_files or {})))
        context, _ = call_context(dict(value=value))
        return resolve_resource_calls(contextual(inspector, context), entry, candidate)

    def test_original_double_enumeration_recursion_union_and_unique_selection(self):
        trace = self.trace()
        self.assertEqual(trace['unresolved_targets'], [], trace)
        self.assertEqual(trace['negative_scope_status'], 'CHECKED', trace)
        facts = [p for p in trace['external_protocols'] if p['rule'] == 'source-complete-finite-class-world/1']
        self.assertTrue(facts)
        self.assertTrue(all(not p['target_requirements']['execution_permission'] for p in facts))
        self.assertTrue(any(p.get('result_order') == 'UNKNOWN_SET_ORDER' for p in facts))
        self.assertTrue(any(p.get('matched') is True and p.get('selected_name') == 'Leaf' for p in facts))
        self.assertEqual({site['symbol'] for site in trace['invoked_callables']},
                         {'score', 'Selector.choose', 'Selector.members'})
        self.assertTrue(all({'start', 'end', 'col', 'endcol', 'ast_sha256', 'kind'} <= occurrence['call_site'].keys()
                            for occurrence in trace['invoked_calls']))
        self.assertTrue(all('call_site' in fact for fact in facts))
        calls = [p for p in facts if p.get('operation') == 'original ordered native direct subclass enumeration']
        self.assertGreaterEqual(len(calls), 8)

    def test_no_match_remains_the_original_following_path(self):
        trace = self.trace(value='Unknown')
        self.assertEqual(trace['negative_scope_status'], 'CHECKED', trace)
        self.assertTrue(any(p.get('matched') is False for p in trace['external_protocols']))

    def test_unknown_query_set_escape_and_order_sensitive_effects_remain_unknown(self):
        sources = [
            SOURCE.replace('return selected.__name__', 'return Selector.members()'),
            SOURCE.replace('if name == member.__name__:', 'if member.__name__ == name:\n                external(member)'),
            SOURCE.replace('if name == member.__name__:\n                return member', 'return member'),
            SOURCE.replace('for s in Selector.members(c)', 'for s in external(c)'),
        ]
        for source in sources:
            with self.subTest(source=source):
                trace = self.trace(source)
                self.assertTrue(trace['unresolved_targets'] or trace['negative_scope_status'] == 'UNKNOWN', trace)

    def test_non_descending_recursion_other_state_change_and_unknown_creation_reject(self):
        sources = [
            SOURCE.replace('Selector.members(c)', 'Selector.members(cls)'),
            SOURCE.replace('def members(cls=None):', 'def members(cls=None, state=0):').replace('Selector.members(c)', 'Selector.members(c, state+1)'),
            SOURCE + '\ndef future():\n    class Added(Root):\n        pass\n',
            SOURCE.replace('class Leaf(Left):', 'class Leaf(Left, Right):'),
        ]
        for source in sources:
            with self.subTest(source=source):
                trace = self.trace(source)
                self.assertTrue(trace['unresolved_targets'] or trace['negative_scope_status'] == 'UNKNOWN', trace)

    def test_unordered_comprehension_cannot_execute_a_projection_callback(self):
        source = SOURCE.replace('members = Selector.members()', 'members = set([external(s) for s in Selector.members()])')
        trace = self.trace(source)
        self.assertEqual(trace['negative_scope_status'], 'UNKNOWN', trace)

    def test_unrelated_enumeration_root_requires_its_own_complete_target_world(self):
        source = SOURCE.replace('class Selector:', 'class Other:\n    pass\nclass OtherChild(Other):\n    pass\nclass Selector:')
        source = source.replace('selected = Selector.choose(value)',
                                'selected = Selector.choose(value)\n    other = set(Other.__subclasses__())')
        trace = self.trace(source)
        self.assertEqual(trace['negative_scope_status'], 'CHECKED', trace)
        roots = {p['target_requirements']['root'] for p in trace['external_protocols']
                 if p['rule'] == 'source-complete-finite-class-world/1'}
        self.assertEqual(roots, {'original.py:Root', 'original.py:Other'})

    def test_same_name_in_distinct_original_modules_cannot_choose_a_set_first_match(self):
        source = SOURCE.replace('class Selector:', 'from child import Left as ForeignLeft\nclass Selector:')
        trace = self.trace(source, value='Left', other_files={
            'child.py': 'from original import Root\nclass Left(Root):\n    pass\n'})
        self.assertEqual(trace['negative_scope_status'], 'UNKNOWN', trace)
        self.assertTrue(any('duplicate native class names' in gap['reason'] for gap in trace['negative_scope_gaps']), trace)

    def test_source_closed_registry_still_requires_an_enforced_actual_target_recipe(self):
        check = fixture.ActivationPrefixTests('test_parent_package_runs_before_child_and_is_not_repeated')
        self.addCleanup(check.doCleanups)
        inspector, entry, candidate = check.fixture({'original.py': SOURCE})
        entry['fixed_arguments'] = dict(value='Leaf')
        recipe = compile_condition(inspector, entry, candidate)
        self.assertIsNotNone(recipe)
        self.assertEqual(recipe['status'], 'REQUIRES_ACTUAL_CALL')
        self.assertEqual(recipe['global_negative_scope'], 'CHECKED')
        self.assertEqual(recipe['initialization']['status'], 'CHECKED')
        self.assertTrue(recipe['runtime_protocol_requirements'])
        self.assertTrue(all(not fact['execution_permission'] for fact in recipe['runtime_protocol_requirements']))

    def test_constructor_using_registry_cannot_gain_unchecked_initialization_permission(self):
        source = SOURCE + '''
class Owner:
    def __init__(self):
        self.choice = Selector.choose('Leaf')
    def score(self, value):
        return self.choice.__name__
'''
        check = fixture.ActivationPrefixTests('test_parent_package_runs_before_child_and_is_not_repeated')
        self.addCleanup(check.doCleanups)
        inspector, entry, candidate = check.fixture({'original.py': source}, symbol='Owner.score', kind='method')
        recipe = compile_condition(inspector, entry, candidate)
        self.assertIsNotNone(recipe)
        self.assertEqual(recipe['initialization']['status'], 'UNKNOWN')

    def test_nested_public_class_collections_and_class_references_do_not_escape(self):
        for result in ("{'members': Selector.members()}", '[Selector.members()]',
                       "[{'member': selected}]", 'selected'):
            with self.subTest(result=result):
                trace = self.trace(SOURCE.replace('return selected.__name__', 'return ' + result))
                self.assertEqual(trace['negative_scope_status'], 'UNKNOWN', trace)
                self.assertTrue(any('escapes the checked synchronous' in gap['reason'] for gap in trace['negative_scope_gaps']))

    def test_no_match_cannot_erase_the_original_unknown_final_loop_target(self):
        source = SOURCE.replace('        return None', '        return member')
        source = source.replace('return selected.__name__', 'print(selected)\n        return selected.__name__')
        trace = self.trace(source, value='Unknown')
        self.assertEqual(trace['negative_scope_status'], 'UNKNOWN', trace)
        self.assertTrue(any('unknown original final loop target' in gap['reason'] for gap in trace['negative_scope_gaps']))

    def test_loop_target_is_not_a_stable_query_from_its_prior_binding(self):
        source = SOURCE.replace('members = Selector.members()', 'members = Selector.members()\n        member = name')
        source = source.replace('if name == member.__name__:', 'if member == member.__name__:')
        trace = self.trace(source)
        self.assertEqual(trace['negative_scope_status'], 'UNKNOWN', trace)
        self.assertFalse(any(p.get('matched') is True for p in trace['external_protocols']))

    def test_source_instance_field_cannot_hide_a_class_collection_escape(self):
        source = SOURCE.replace('class Selector:', '''class Box:
    def __init__(self, members):
        self.members = members
class Selector:''')
        source = source.replace('return selected.__name__', 'return Box(Selector.members())')
        trace = self.trace(source)
        self.assertEqual(trace['negative_scope_status'], 'UNKNOWN', trace)
        self.assertTrue(any('escapes the checked synchronous' in gap['reason'] for gap in trace['negative_scope_gaps']))

    def test_source_closure_cannot_hide_a_class_collection_escape(self):
        source = SOURCE.replace('return selected.__name__', '''members = Selector.members()
        def retained():
            return members
        return retained''')
        trace = self.trace(source)
        self.assertEqual(trace['negative_scope_status'], 'UNKNOWN', trace)
        self.assertTrue(any('escapes the checked synchronous' in gap['reason'] for gap in trace['negative_scope_gaps']))


if __name__ == '__main__':
    unittest.main()
