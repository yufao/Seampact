"""Configured generic LLM slots bind local service identities, not arbitrary models."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from seampact.capability_generation import GenerationJobs, authorized_text_first_call_services, resolve_resources


class Inspector:
    def __init__(self, source):
        self.source = source
        self.request = dict(configuration=dict(resource_choices=dict(checker="local-connection")))

    def data(self, path):
        return self.source.encode()


class Profiles:
    def __init__(self):
        self.profile = dict(id="local-connection", model="selected-chat", protocol="openai-compatible",
                            base_url="http://local.invalid", parameters={}, service_capabilities=["text"],
                            resource_kind="external", auth=dict(mode="none"))

    def connection_profile(self, identity):
        if identity != self.profile["id"]:
            raise ValueError("unknown connection")
        return self.profile

    def connection_key(self, identity):
        return ""


class ServiceChoices(unittest.TestCase):
    def setUp(self):
        self.profiles = Profiles()
        self.platform = SimpleNamespace(profiles=self.profiles)
        self.manifest = dict(resources=[dict(id="checker", kind="service", required=True, description="check model")])
        self.selection = dict(checker=dict(configuration_key="checker", operation="text", parameters={},
                                           provenance=[dict(path="module.py", start=1, end=3)]))

    def check(self, source):
        return resolve_resources(self.platform, self.manifest, self.selection, Inspector(source))

    def test_generic_llm_and_typed_llm_slots_bind_selected_connection(self):
        for source in ('class LLMExtractor:\n    def __init__(self, model, api_base=None):\n        pass\n',
                       'def self_check_input(llm: LLMModel):\n    pass\n'):
            with self.subTest(source=source):
                bound, gaps, _ = self.check(source)
                self.assertFalse(gaps)
                self.assertEqual(bound['checker']['model'], 'selected-chat')

    def test_fixed_models_and_wrong_connection_contract_remain_unbound(self):
        fixed = 'class NLIChecker:\n    def __init__(self, model, api_base=None):\n        pass\n'
        self.assertTrue(self.check(fixed)[1])
        generic = 'class LLMExtractor:\n    def __init__(self, model, api_base=None):\n        pass\n'
        for mutation in (dict(service_capabilities=['embedding']), dict(protocol='http'), dict(parameters={'temperature':0})):
            with self.subTest(mutation=mutation):
                old = self.profiles.profile.copy()
                self.profiles.profile.update(mutation)
                self.assertTrue(self.check(generic)[1])
                self.profiles.profile = old
        self.selection['checker']['operation'] = 'embedding'
        self.assertTrue(self.check(generic)[1])

    def test_first_call_only_allows_explicit_bound_text_service_and_synthetic_opt_in(self):
        inspector = Inspector('class LLMExtractor:\n    def __init__(self, model, api_base=None):\n        pass\n')
        binding, gaps, _ = resolve_resources(self.platform, self.manifest, self.selection, inspector)
        self.assertFalse(gaps)
        capability = dict(resources=['checker'], effects=[dict(kind='network', access='write', target='resource:checker')])
        allowed = authorized_text_first_call_services(self.platform, inspector, self.selection, binding)
        self.assertTrue(GenerationJobs._sample_gap(capability, binding, allowed))
        inspector.request['configuration']['allow_synthetic_service_first_call'] = True
        allowed = authorized_text_first_call_services(self.platform, inspector, self.selection, binding)
        self.assertIsNone(GenerationJobs._sample_gap(capability, binding, allowed))
        arbitrary = dict(resources=['checker'], effects=[dict(kind='network', access='write', target='https://unrelated.invalid')])
        self.assertTrue(GenerationJobs._sample_gap(arbitrary, binding, allowed))
        external = dict(resources=['checker'], effects=[dict(kind='external', access='write', target='resource:checker')])
        self.assertTrue(GenerationJobs._sample_gap(external, binding, allowed))
        self.profiles.profile['parameters'] = {'temperature': 0}
        self.assertFalse(authorized_text_first_call_services(self.platform, inspector, self.selection, binding))


if __name__ == '__main__':
    unittest.main()
