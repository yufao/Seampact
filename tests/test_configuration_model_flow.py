"""Normal source replay retains original model callback and export operations."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.call_value_conditions import call_context, contextual, digest
from seampact.source.dependency_closure import resolve_resource_calls
from seampact.source.configuration_model_flow import GAP
from seampact.source.pydantic_models import RULE
import test_activation_prefix as fixtures


SOURCE = '''from typing import Optional
from pydantic import BaseModel, Field, model_validator, field_validator
import consumer
class Config(BaseModel):
    name: str = Field(...)
    enabled: bool = Field(False)
    optional: Optional[str] = Field(None)
    @model_validator(mode='before')
    @classmethod
    def before(cls, data):
        consumer.before(data['name'])
        return data
    @field_validator('name')
    @classmethod
    def field(cls, value):
        consumer.field(value)
        return value
    @model_validator(mode='after')
    def after(self):
        consumer.after(self.name, self.enabled)
        return self
class Child(Config):
    def model_dump(self, *args, **kwargs):
        kwargs.setdefault('exclude_none', True)
        return super().model_dump(*args, **kwargs)
def score(value):
    configured = Child(name=value)
    exported = configured.model_dump()
    return consumer.finish(exported)
'''


class ConfigurationModelFlowTests(unittest.TestCase):
    def trace(self, source=SOURCE):
        case = fixtures.ActivationPrefixTests('test_parent_package_runs_before_child_and_is_not_repeated')
        self.addCleanup(case.doCleanups)
        inspector, entry, candidate = case.fixture({'original.py': source})
        return resolve_resource_calls(contextual(inspector, call_context({'value': 'original'})[0]), entry, candidate)

    def test_normal_constructor_replays_inherited_before_field_after_and_original_dump(self):
        trace = self.trace()
        names = [row['target'] for row in trace['calls']]
        self.assertEqual([name for name in names if name.startswith('consumer.')],
                         ['consumer.before', 'consumer.field', 'consumer.after', 'consumer.finish'])
        result = next(row for row in trace['calls'] if row['target'] == 'consumer.finish')
        self.assertEqual(result['args'][0], {'status': 'CHECKED', 'value': {'name': 'original', 'enabled': False}})
        self.assertIn('Child.model_dump', {row['symbol'] for row in trace['invoked_callables']})
        self.assertTrue(any(row['rule'] == RULE and row.get('operation') == 'original native model_dump'
                            for row in trace['external_protocols']))
        self.assertTrue(any(row['reason'] == GAP for row in trace['negative_scope_gaps']))
        self.assertNotEqual(trace['activation_prefix']['status'], 'CHECKED')

    def test_disabled_model_is_still_validated_and_absent_field_callback_is_not_invented(self):
        trace = self.trace(SOURCE.replace('optional: Optional[str] = Field(None)',
            "optional: Optional[str] = Field(None)\n    @field_validator('optional')\n    @classmethod\n    def absent(cls, value):\n        consumer.absent(value)\n        return value"))
        self.assertTrue(any(row['target'] == 'consumer.after' and row['args'][1]['value'] is False for row in trace['calls']))
        self.assertFalse(any(row['target'] == 'consumer.absent' for row in trace['calls']))

    def test_callback_replacing_self_and_unknown_dump_option_keep_unknown(self):
        for source in (SOURCE.replace('return self', "return {'changed': True}"),
                       SOURCE.replace('configured.model_dump()', 'configured.model_dump(mode=consumer.mode())')):
            with self.subTest(source=source):
                trace = self.trace(source)
                result = next(row for row in trace['calls'] if row['target'] == 'consumer.finish')
                self.assertEqual(result['args'][0]['status'], 'UNKNOWN')

    def test_normal_catalog_does_not_activate_unproved_original_sdk_prefix(self):
        from seampact.runtime import Coordinator, OwnerRuntime
        from seampact.store import Store
        from seampact.units import UnitCatalog, NativeBindings
        case = fixtures.ActivationPrefixTests('test_parent_package_runs_before_child_and_is_not_repeated')
        self.addCleanup(case.doCleanups)
        source = "from pydantic import BaseModel, Field\nclass Config(BaseModel):\n    name: str = Field(...)\ndef score(value):\n    return Config(name=value).name\n"
        inspector, entry, candidate = case.fixture({'original.py': source})
        inspector.request['source_id'] = 'source'
        store = Store(':memory:')
        self.addCleanup(store.close)
        runtime = OwnerRuntime(Coordinator(store))
        self.addCleanup(runtime.close)
        catalog = UnitCatalog(inspector.root, store, runtime)
        before = {name for name in sys.modules if name == 'pydantic' or name.startswith('pydantic.')}
        with patch.object(NativeBindings, '_load', side_effect=AssertionError('original owner/model load is forbidden')):
            unit = catalog.install(inspector, candidate, 'analysis', 'installation')[0]
            self.assertEqual(unit['status'], 'MISSING', unit)
            with self.assertRaises(ValueError):
                catalog.invoke(unit['id'], {'value': 'original'})
        after = {name for name in sys.modules if name == 'pydantic' or name.startswith('pydantic.')}
        self.assertEqual(before, after)

    def test_only_internal_exact_target_receipt_discharges_own_gap(self):
        case = fixtures.ActivationPrefixTests('test_parent_package_runs_before_child_and_is_not_repeated')
        self.addCleanup(case.doCleanups)
        inspector, entry, candidate = case.fixture({'original.py': SOURCE})
        initial = self.trace()
        plan = next(row['target_requirements'] for row in initial['external_protocols']
                    if row['rule'] == RULE and row.get('operation') == 'original finite model construction')
        candidate['runtime_verified'] = [digest(plan)]  # Candidate metadata never feeds internal evidence.
        context, _ = call_context({'value': 'original'})
        ordinary = resolve_resource_calls(contextual(inspector, context), entry, candidate)
        self.assertTrue(any(row['reason'] == GAP for row in ordinary['negative_scope_gaps']))
        context.runtime_verified = (plan['digest'],)  # The embedded source hash is not the complete requirement digest.
        wrong = resolve_resource_calls(contextual(inspector, context), entry, candidate)
        self.assertTrue(any(row['reason'] == GAP for row in wrong['negative_scope_gaps']))
        context.runtime_verified = (digest(plan),)
        checked = resolve_resource_calls(contextual(inspector, context), entry, candidate)
        self.assertFalse(any(row['reason'] == GAP for row in checked['negative_scope_gaps']))
        self.assertEqual(checked['activation_prefix']['status'], 'UNKNOWN')
        from seampact.source.import_only import compile_import_only_plan
        plan = compile_import_only_plan(inspector, entry, candidate, {'requirements': [], 'bindings': {}})
        self.assertEqual(plan['status'], 'UNKNOWN')

    def test_inherited_callback_order_matches_original_pydantic_schema_nesting(self):
        # Pydantic 2.14.3 DecoratorInfos.build collects base then subclass.
        # apply_model_validators/_apply_annotations wrap each new callback
        # around the existing schema: before runs outside-in, after inside-out.
        classes = []
        for owner, base in (('Base', 'BaseModel'), ('Child', 'Base')):
            declarations = ['class ' + owner + '(' + base + '):']
            if owner == 'Base':
                declarations.append('    name: str = Field(...)')
            for kind, mode in (('model', 'before'), ('field', 'before'), ('field', 'after'), ('model', 'after')):
                for number in (1, 2):
                    name = owner.lower() + '_' + kind + '_' + mode + str(number)
                    decorator = "model_validator(mode='" + mode + "')" if kind == 'model' else "field_validator('name', mode='" + mode + "')"
                    declarations.append('    @' + decorator)
                    if kind == 'model' and mode == 'after':
                        declarations.extend(['    def ' + name + '(self):', '        consumer.' + name + '()', '        return self'])
                    else:
                        declarations.extend(['    @classmethod', '    def ' + name + '(cls, value):',
                                             '        consumer.' + name + '()', '        return value'])
            classes.extend(declarations)
        source = ('from pydantic import BaseModel, Field, model_validator, field_validator\nimport consumer\n'
                  + '\n'.join(classes) + '\ndef score(value):\n    return Child(name=value).name\n')
        trace = self.trace(source)
        observed = [row['target'].removeprefix('consumer.') for row in trace['calls'] if row['target'].startswith('consumer.')]
        expected = []
        for kind, mode in (('model', 'before'), ('field', 'before'), ('field', 'after'), ('model', 'after')):
            owners, numbers = (('child', 'base'), (2, 1)) if mode == 'before' else (('base', 'child'), (1, 2))
            expected.extend(owner + '_' + kind + '_' + mode + str(number) for owner in owners for number in numbers)
        self.assertEqual(observed, expected)


if __name__ == '__main__':
    unittest.main()
