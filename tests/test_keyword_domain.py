"""A narrowed original kwargs domain is enforced by the real catalog path."""
import ast
import copy
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.keyword_domain import literal_equal, narrow_keywords
from seampact.source.inspection import Inspector
from seampact.runtime import Coordinator, OwnerRuntime
from seampact.store import Store
from seampact.units import UnitCatalog

SOURCE='def echo(text: str, **kwargs) -> dict[str, str]:\n    return {"text": text, **kwargs}\n'


class KeywordDomainTests(unittest.TestCase):
    def compile(self, domain, source=SOURCE, fixed=()):
        node=ast.parse(source).body[0]
        schema=dict(type='object',properties={'text':{'type':'string'}},required=['text'],additionalProperties=True)
        return narrow_keywords(schema,node,domain,fixed)

    def catalog(self, domain, source=SOURCE):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);root=Path(tmp.name)
        (root/'original.py').write_text(source,encoding='utf-8')
        inspector=Inspector.freeze(root,root/'snapshots','request',['original.py'],'echo','original output')
        inspector.request['source_id']='source'
        entry=dict(id='echo',path='original.py',symbol='echo',kind='function',boundary_ids=['echo-W'],keyword_domain=domain)
        candidate=dict(entries=[entry],boundaries=[dict(id='echo-W',path='original.py',symbol='echo',mode='W')])
        store=Store(':memory:');self.addCleanup(store.close)
        runtime=OwnerRuntime(Coordinator(store));self.addCleanup(runtime.close)
        catalog=UnitCatalog(root,store,runtime)
        report=catalog.inspect_candidate(inspector,candidate)
        return catalog,inspector,candidate,report

    def test_original_kwargs_are_passed_unchanged_without_injected_defaults(self):
        catalog,inspector,candidate,report=self.catalog({'mode':['safe']})
        self.assertEqual(report['entries'][0]['status'],'READY',report['entries'][0].get('reason'))
        unit=catalog.install(inspector,candidate,'analysis','install')[0]
        self.assertEqual(catalog.invoke(unit['id'],{'text':'hello'}),{'text':'hello'})
        self.assertEqual(catalog.invoke(unit['id'],{'text':'hello','mode':'safe'}),{'text':'hello','mode':'safe'})
        for args in ({'text':'hello','mode':'other'},{'text':'hello','callback':'unexpected'}):
            with self.assertRaises(ValueError):catalog.invoke(unit['id'],args)
        group=catalog.store.get('runtime_group',unit['runtime_group'])
        self.assertEqual(group['contract']['lifecycle']['replicate']['status'],'UNKNOWN')

    def test_empty_domain_closes_only_the_variadic_arm(self):
        schema=self.compile({})
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(set(schema['properties']),{'text'})
        self.assertEqual(schema['required'],['text'])

    def test_domain_is_checked_and_frozen_in_the_original_group_contract(self):
        catalog,inspector,candidate,report=self.catalog({'mode':['safe']})
        original=report['runtime_groups']['groups'][0]['contract']['digest']
        changed=copy.deepcopy(candidate);changed['entries'][0]['keyword_domain']={'mode':['other']}
        other=catalog.inspect_candidate(inspector,changed)
        self.assertNotEqual(original,other['runtime_groups']['groups'][0]['contract']['digest'])
        _,_,_,invalid=self.catalog({'text':['override']})
        self.assertEqual(invalid['entries'][0]['status'],'MISSING')

    def test_nonvariadic_original_entry_cannot_gain_a_keyword(self):
        with self.assertRaisesRegex(ValueError,'original'):
            self.compile({'mode':['safe']},'def echo(text: str) -> str:\n    return text\n')

    def test_explicit_capture_and_fixed_names_cannot_be_redefined(self):
        for domain,fixed in [({'text':['other']},()),({'kwargs':[{}]},()),({'api_key':['literal']},['api_key'])]:
            with self.assertRaises(ValueError):self.compile(domain,fixed=fixed)

    def test_domain_is_a_detached_finite_json_value_set(self):
        choices={'extra_body':[{'thinking':{'type':'disabled'}}]}
        schema=self.compile(choices)
        choices['extra_body'][0]['thinking']['type']='enabled'
        self.assertEqual(schema['properties']['extra_body']['enum'][0]['thinking']['type'],'disabled')
        for bad in (None,[],{'mode':[]},{'mode':['safe','safe']},{'mode':[float('nan')]},{'mode':[object()]},
                    {'__class__':['anything']},{'bad-name':['anything']}):
            with self.subTest(bad=type(bad).__name__):
                with self.assertRaises(ValueError):self.compile(bad)

    def test_json_equality_is_strict_at_nested_boolean_and_number_fields(self):
        self.assertFalse(literal_equal({'enabled':True},{'enabled':1}))
        self.assertFalse(literal_equal([1],[1.0]))
        self.assertFalse(literal_equal(float('nan'),float('nan')))
        self.assertTrue(literal_equal({'a':1,'b':['x']},{'b':['x'],'a':1}))


if __name__=='__main__':unittest.main()
