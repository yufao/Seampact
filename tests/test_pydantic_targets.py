"""Opaque model targets are borrowed only within the original read/use region."""
import gc
import sys
import types
import unittest
import weakref
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.pydantic_models import PydanticUnknown, compile_model_plan
from seampact.source.pydantic_targets import (
    CreationObservation, DeferredCreationObservation, PythonCreationAnchor, required_models, verify_model_targets,
)
import test_pydantic_models as model_fixtures


SDK = '''_plugins = None
_loading_plugins = False
class Meta(type):
    pass
class Validator:
    __slots__ = ('schema', 'config')
    def __init__(self, schema, config):
        self.schema, self.config = schema, config
class SchemaSerializer:
    __slots__ = ('schema', 'config')
    def __init__(self, schema, config):
        self.schema, self.config = schema, config
def get_plugins():
    disabled_plugins = None
    global _plugins
    if _plugins is None:
        _plugins = {}
    return _plugins.values()
def create_schema_validator(schema, schema_type, config=None):
    plugins = get_plugins()
    return Validator(schema, config)
def complete_model_class(cls):
    schema = {'cls': cls}
    core_config = {}
    cls.__pydantic_core_schema__ = schema
    cls.__pydantic_validator__ = create_schema_validator(schema, cls, core_config)
    cls.__pydantic_serializer__ = SchemaSerializer(schema, core_config)
    return True
'''


class PydanticTargetTests(unittest.TestCase):
    def fixture(self, source=SDK):
        module = types.ModuleType('original_sdk')
        exec(compile(source, 'original_sdk.py', 'exec'), module.__dict__)
        functions = {name: module.__dict__[name] for name in
                     ('complete_model_class', 'create_schema_validator', 'get_plugins')}
        kernel = dict(metaclass=module.Meta, validator=module.Validator, serializer=module.SchemaSerializer)
        return module, CreationObservation(functions, kernel)

    def test_before_exec_association_multiple_classes_and_exact_binding_drift(self):
        sdk, observation = self.fixture()
        with observation:
            first = sdk.Meta('First', (), {})
            second = sdk.Meta('Second', (), {})
            sdk.complete_model_class(first)
            sdk.complete_model_class(second)
            answer = observation.verify(first)
            self.assertFalse(answer['execution_permission'])
            self.assertFalse(answer['cross_region_qualification'])
            self.assertTrue(observation.verify(second))
            first.__pydantic_validator__ = sdk.Validator(first.__pydantic_core_schema__, {})
            with self.assertRaisesRegex(PydanticUnknown, 'binding changed'):
                observation.verify(first)
        with self.assertRaisesRegex(PydanticUnknown, 'cross-region'):
            observation.verify(second)
        self.assertEqual(observation._models, [])
        self.assertEqual(observation._validators, [])

    def test_existing_empty_plugin_cache_does_not_prove_creation_provenance(self):
        sdk, observation = self.fixture()
        sdk._plugins = {}
        with observation:
            model = sdk.Meta('Model', (), {})
            sdk.complete_model_class(model)  # Original return remains intact.
            with self.assertRaisesRegex(PydanticUnknown, 'no-plugin'):
                observation.verify(model)

    def test_plugin_discovery_iteration_disable_and_unknown_plugin_states_reject(self):
        variants = [SDK.replace('disabled_plugins = None', "disabled_plugins = '__all__'"),
                    SDK.replace('return _plugins.values()', "entry_point = 'even_failed_plugin'\n    return _plugins.values()"),
                    SDK.replace('_loading_plugins = False', '_loading_plugins = True')]
        for source in variants:
            with self.subTest(source=source):
                sdk, observation = self.fixture(source)
                with observation:
                    model = sdk.Meta('Model', (), {})
                    self.assertTrue(sdk.complete_model_class(model))
                    with self.assertRaisesRegex(PydanticUnknown, 'no-plugin'):
                        observation.verify(model)

    def test_posthoc_capture_rebuild_and_other_global_function_reject(self):
        sdk, observation = self.fixture()
        model = sdk.Meta('Model', (), {})
        sdk.complete_model_class(model)
        with observation:
            with self.assertRaisesRegex(PydanticUnknown, 'no original native creation'):
                observation.verify(model)
        sdk, observation = self.fixture()
        with observation:
            model = sdk.Meta('Model', (), {})
            sdk.complete_model_class(model)
            sdk.complete_model_class(model)
            with self.assertRaisesRegex(PydanticUnknown, 'repeated/rebuilt'):
                observation.verify(model)

    def test_unknown_metaclass_attribute_hooks_are_not_invoked(self):
        sdk, observation = self.fixture()
        hits = []
        class UnknownMeta(type):
            def __getattribute__(cls, name):
                hits.append(name)
                return type.__getattribute__(cls, name)
        with observation:
            unknown = UnknownMeta('Unknown', (), {})
            with self.assertRaises(PydanticUnknown):
                observation.verify(unknown)
        self.assertEqual(hits, [])

    def test_exit_drops_borrowed_opaque_targets_and_does_not_pin_class(self):
        sdk, observation = self.fixture()
        with observation:
            model = sdk.Meta('Model', (), {})
            sdk.OriginalModel = model  # Original module owner retains it throughout the checked interval.
            sdk.complete_model_class(model)
            reference = weakref.ref(model)
            with self.assertRaises(TypeError):
                weakref.ref(model.__pydantic_validator__)
            del model
            gc.collect()
            self.assertIsNotNone(reference())
        gc.collect()
        self.assertIsNotNone(reference())
        del sdk.OriginalModel  # Observer cleared before the original owner releases the class.
        gc.collect()
        self.assertIsNone(reference())

    def test_existing_observer_is_preserved_and_abnormal_exit_clears_native_refs(self):
        sdk, observation = self.fixture()
        callback = lambda *args: None
        sys.setprofile(callback)
        try:
            with self.assertRaises(PydanticUnknown):
                observation.__enter__()
            self.assertIs(sys.getprofile(), callback)
        finally:
            sys.setprofile(None)
        with self.assertRaisesRegex(RuntimeError, 'original failure'):
            with observation:
                model = sdk.Meta('Model', (), {})
                sdk.complete_model_class(model)
                raise RuntimeError('original failure')
        self.assertIsNone(sys.getprofile())
        self.assertEqual(observation._models, [])

    def test_failed_creation_association_clears_borrows_before_original_continuation(self):
        sdk, observation = self.fixture(SDK.replace('return True', 'return False'))
        with observation:
            model = sdk.Meta('Original', (), {})
            self.assertFalse(sdk.complete_model_class(model))
            self.assertEqual(observation._models, [])
            self.assertEqual(observation._validators, [])
            self.assertEqual(observation._pending, [])
            with self.assertRaises(PydanticUnknown):
                observation.verify(model)

    def test_source_only_requirement_cannot_become_normal_worker_permission(self):
        case = model_fixtures.PydanticModelPlanTests('test_plan_is_immutable_and_source_bound')
        self.addCleanup(case.doCleanups)
        inspector = case.fixture()
        plan = compile_model_plan(inspector, 'original.py', 'Root').requirements()
        trace = dict(external_protocols=[dict(rule=plan['schema'], target_requirements=plan)])
        self.assertTrue(required_models(inspector, trace))
        entry = {'id': 'target', 'kind': 'function', 'path': 'original.py', 'symbol': 'score'}
        with self.assertRaisesRegex(PydanticUnknown, 'read-use source effects'):
            verify_model_targets(inspector, entry, {'entries': [entry]}, trace, None, {})

    def deferred(self):
        source = SDK.replace('Meta', 'ModelMetaclass').replace('Validator', 'SchemaValidator')
        names = ('complete_model_class', 'create_schema_validator', 'get_plugins')
        anchors = {name: PythonCreationAnchor.from_source(source, 'original_sdk.py', name) for name in names}
        module = types.ModuleType('original_sdk')
        original = sys.modules.get('original_sdk')
        self.addCleanup(lambda: sys.modules.pop('original_sdk', None) if original is None else sys.modules.__setitem__('original_sdk', original))
        sys.modules['original_sdk'] = module
        return source, module, DeferredCreationObservation(anchors)

    def test_before_exec_deferred_frames_preserve_original_definition_order(self):
        source, sdk, observation = self.deferred()
        self.assertNotIn('get_plugins', sdk.__dict__)
        self.assertEqual(observation._functions, {})
        with observation:
            exec(compile(source, 'original_sdk.py', 'exec'), sdk.__dict__)
            self.assertEqual(observation._functions, {})  # Definitions do not execute callbacks.
            model = sdk.ModelMetaclass('Original', (), {})
            sdk.complete_model_class(model)
            self.assertEqual(set(observation._functions), {'complete_model_class', 'create_schema_validator', 'get_plugins'})
            self.assertFalse(observation.verify(model)['execution_permission'])
        self.assertEqual(observation._models, [])
        with self.assertRaisesRegex(PydanticUnknown, 'cross-region'):
            observation.verify(model)

    def test_passive_code_anchor_never_compares_or_hashes_an_opaque_constant(self):
        source, sdk, observation = self.deferred()
        hits = []
        class Opaque:
            def __eq__(self, other): hits.append('eq'); return True
            def __hash__(self): hits.append('hash'); return 0
        with observation:
            exec(compile(source, 'original_sdk.py', 'exec'), sdk.__dict__)
            function = sdk.complete_model_class
            function.__code__ = function.__code__.replace(co_consts=function.__code__.co_consts + (Opaque(),))
            model = sdk.ModelMetaclass('Original', (), {})
            self.assertTrue(sdk.complete_model_class(model))
            with self.assertRaises(PydanticUnknown):
                observation.verify(model)
        self.assertEqual(hits, [])

    def test_independent_target_precheck_retains_only_own_gap_until_actual_targets(self):
        from seampact.source.configuration_model_flow import GAP
        case = model_fixtures.PydanticModelPlanTests('test_plan_is_immutable_and_source_bound')
        self.addCleanup(case.doCleanups)
        inspector = case.fixture()
        plan = compile_model_plan(inspector, 'original.py', 'Root').requirements()
        trace = dict(status='CHECKED', unresolved_targets=[], negative_scope_status='UNKNOWN',
            negative_scope_gaps=[dict(reason=GAP)], activation_prefix=dict(status='CHECKED'),
            external_protocols=[dict(rule=plan['schema'], target_requirements=plan)])
        entry = {'id': 'target', 'kind': 'function', 'path': 'original.py', 'symbol': 'score'}
        with patch('seampact.source.dependency_closure.resolve_resource_calls', return_value=trace):
            with self.assertRaisesRegex(PydanticUnknown, 'provenance remains UNKNOWN'):
                verify_model_targets(inspector, entry, {'entries': [entry]}, trace, None, {})
        trace['negative_scope_gaps'].append(dict(reason='unproved callback'))
        with patch('seampact.source.dependency_closure.resolve_resource_calls', return_value=trace):
            with self.assertRaisesRegex(PydanticUnknown, 'read-use source effects'):
                verify_model_targets(inspector, entry, {'entries': [entry]}, trace, None, {})


if __name__ == '__main__':
    unittest.main()
