"""Original model declarations are facts, never reconstructed validators."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.pydantic_models import compile_model_plan, PydanticUnknown
import test_activation_prefix as fixtures


SOURCE = '''from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
class Context(BaseModel):
    language: str = Field(...)
    @field_validator('language')
    @classmethod
    def language_code(cls, value):
        return value
class Base(BaseModel):
    enabled: bool = Field(True)
    name: str = Field(...)
    @model_validator(mode='before')
    @classmethod
    def before(cls, data):
        return data
    @model_validator(mode='after')
    def after(self):
        return self
class Child(Base):
    contexts: Optional[List[Context]] = Field(None)
    model_config = ConfigDict(extra='allow')
    def model_dump(self, *args, **kwargs):
        kwargs.setdefault('exclude_none', True)
        return super().model_dump(*args, **kwargs)
class Root(BaseModel):
    items: List[Union[Child, str]] = Field(default_factory=list)
    @field_validator('items', mode='before')
    @classmethod
    def parse(cls, values):
        return [Child(**value) for value in values]
def score(value):
    return value
'''


class PydanticModelPlanTests(unittest.TestCase):
    def fixture(self, source=SOURCE):
        case = fixtures.ActivationPrefixTests('test_parent_package_runs_before_child_and_is_not_repeated')
        self.addCleanup(case.doCleanups)
        return case.fixture({'original.py': source})[0]

    def test_nested_and_inherited_original_declarations_are_retained(self):
        inspector = self.fixture()
        plan = compile_model_plan(inspector, 'original.py', 'Root').requirements()
        self.assertFalse(plan['execution_permission'])
        self.assertEqual(plan['lifecycle_permissions'], [])
        rows = {row['symbol']: row for row in plan['models']}
        self.assertEqual(set(rows), {'Root', 'Child', 'Base', 'Context'})
        self.assertEqual(rows['Child']['field_order'], ['enabled', 'name', 'contexts'])
        self.assertEqual(rows['Child']['method_order'], ['before', 'after', 'model_dump'])
        self.assertEqual(rows['Child']['base'], 'original.py:Base')
        self.assertEqual(rows['Child']['config'], {'extra': 'allow'})
        self.assertEqual(rows['Child']['methods']['before']['validators'][0]['mode'], 'before')
        self.assertIn('model_dump', rows['Child']['methods'])
        self.assertEqual(rows['Root']['fields']['items']['field']['default'], {'factory': 'builtins.list'})
        self.assertEqual(rows['Context']['methods']['language_code']['descriptor'], 'classmethod')

    def test_plan_is_immutable_and_source_bound(self):
        inspector = self.fixture()
        compiled = compile_model_plan(inspector, 'original.py', 'Root')
        first = compiled.requirements()
        first['models'].clear()
        self.assertTrue(compiled.requirements()['models'])
        changed = self.fixture(SOURCE.replace('enabled: bool = Field(True)', 'enabled: bool = Field(False)'))
        self.assertNotEqual(compiled.requirements()['digest'], compile_model_plan(changed, 'original.py', 'Root').requirements()['digest'])

    def test_unknown_hooks_factories_decorators_and_dynamic_bases_reject(self):
        variants = [
            SOURCE.replace('enabled: bool = Field(True)', 'enabled: bool = Field(default_factory=unknown)'),
            SOURCE.replace("@model_validator(mode='before')", "@model_validator(mode='wrap')"),
            SOURCE.replace('class Base(BaseModel):', 'class Base(BaseModel, Unknown):'),
            SOURCE.replace('class Base(BaseModel):', 'class Base(BaseModel):\n    def __pydantic_on_complete__(cls): pass'),
            SOURCE.replace('class Base(BaseModel):', 'class Base(BaseModel):\n    @unknown\n    def hook(self): pass'),
            SOURCE.replace('class Base(BaseModel):', 'class Base(BaseModel):\n    dynamic = unknown()'),
        ]
        for source in variants:
            with self.subTest(source=source):
                with self.assertRaises(PydanticUnknown):
                    compile_model_plan(self.fixture(source), 'original.py', 'Root')

    def test_disabled_field_never_eliminates_original_callbacks(self):
        inspector = self.fixture(SOURCE.replace('enabled: bool = Field(True)', 'enabled: bool = Field(False)'))
        plan = compile_model_plan(inspector, 'original.py', 'Child').requirements()
        child = next(row for row in plan['models'] if row['symbol'] == 'Child')
        self.assertIn('before', child['methods'])
        self.assertIn('after', child['methods'])
        self.assertIn('model_dump', child['methods'])
        self.assertEqual(child['fields']['enabled']['field']['default'], {'value': False})

    def test_plain_default_and_default_factory_conflict_rejects_both_orders(self):
        for declaration in ('Field(default=None, default_factory=list)', 'Field(default_factory=list, default=None)'):
            source = SOURCE.replace('Field(default_factory=list)', declaration)
            with self.subTest(declaration=declaration), self.assertRaises(PydanticUnknown):
                compile_model_plan(self.fixture(source), 'original.py', 'Root')

    def test_referenced_complete_factory_includes_disabled_special_model(self):
        source = SOURCE.replace('class Root(BaseModel):', 'class Disabled(Base):\n    enabled: bool = Field(False)\nclass Root(BaseModel):')
        source = source.replace('return [Child(**value) for value in values]',
                                "return [CONFIG_MODEL_MAP.get(value['name'], Child)(**value) for value in values]")
        source += "CONFIG_MODEL_MAP = {'base': Child, 'disabled': Disabled}\n"
        plan = compile_model_plan(self.fixture(source), 'original.py', 'Root').requirements()
        self.assertIn('Disabled', {row['symbol'] for row in plan['models']})
        self.assertEqual([row['key'] for row in plan['factories']['CONFIG_MODEL_MAP']['members']], ['base', 'disabled'])

    def test_shadowed_builtin_and_repeated_class_declarations_reject(self):
        for source in (SOURCE.replace('class Context(BaseModel):', 'str = unknown\nclass Context(BaseModel):'),
                       SOURCE + '\nclass Context(BaseModel): pass\n',
                       SOURCE.replace('class Context(BaseModel):', 'list = unknown\nclass Context(BaseModel):')):
            with self.subTest(source=source), self.assertRaises(PydanticUnknown):
                compile_model_plan(self.fixture(source), 'original.py', 'Root')

    def test_overridden_inherited_validator_cannot_silently_remove_callback(self):
        source = SOURCE.replace('class Child(Base):', 'class Child(Base):\n    def after(self):\n        return self')
        with self.assertRaisesRegex(PydanticUnknown, 'inherited validator'):
            compile_model_plan(self.fixture(source), 'original.py', 'Root')

    def test_module_model_replacement_release_and_function_rebinding_reject(self):
        for suffix in ('\nChild = Base\n', '\ndel Child\n', '\ndef Child(): pass\n'):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(PydanticUnknown, 'replaced or released'):
                compile_model_plan(self.fixture(SOURCE + suffix), 'original.py', 'Root')


if __name__ == '__main__':
    unittest.main()
