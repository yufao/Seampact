"""Actual original targets must still match at the locked point of use."""
import asyncio
import sys
import unittest
from unittest.mock import patch

import test_call_value_conditions as condition_fixture


IDENTITY = dict(session='s', instance_id='i', generation=1, process_nonce='n')


class RuntimeReadUseTests(unittest.TestCase):
    def prepared(self, source, arguments):
        fixture = condition_fixture.CallValueConditionTests(
            'test_catalog_and_worker_require_actual_same_call_before_source_load')
        self.addCleanup(fixture.doCleanups)
        _, _, _, catalog, unit = fixture.catalog(source)
        self.assertEqual(unit['status'], 'CALL_CONDITION_DEFERRED', unit.get('reason'))
        catalog._worker_call_identity = dict(IDENTITY)
        plan = catalog.require_import_only_plan(unit['id'])
        catalog.prepare_import_only(unit['id'], plan['plan_digest'])
        checked = catalog.check_call_conditions(unit['id'], arguments)
        self.assertTrue(checked['allowed'], checked)
        binding, entry = catalog.deferred[unit['id']]
        return binding, entry, unit

    def invoke_after_load_mutation(self, binding, entry, unit, arguments, mutate):
        """Change the original after the first locked check, before its use."""
        original_load = binding._load
        original = binding.import_only_entries[unit['id']]['function']
        called, loads = [], []

        async def changing_load(*args, **kwargs):
            loaded = await original_load(*args, **kwargs)
            loads.append(True)
            mutate()
            # Runs on the owner thread; distinguish actual original execution
            # from the platform's checks without changing the fixture source.
            def profile(frame, event, arg):
                if event == 'call' and frame.f_code is original.__code__:
                    called.append(True)
            sys.setprofile(profile)
            return loaded

        failure = None
        try:
            with patch.object(binding, '_load', changing_load):
                try:
                    binding.invoke(entry, unit, arguments)
                except ValueError as error:
                    failure = error
        finally:
            if binding.loop is not None:
                async def clear_profile():
                    sys.setprofile(None)
                asyncio.run_coroutine_threadsafe(clear_profile(), binding.loop).result(timeout=5)
        self.assertEqual(loads, [True], 'mutation must occur after the first locked check')
        self.assertEqual(called, [], 'the original entry must not execute')
        self.assertIsNotNone(failure, 'changed original target must be rejected')

    def test_generator_helper_and_default_are_rechecked_after_load(self):
        source = ('LIMIT=2\ndef pieces(value,n=2):\n    yield n+LIMIT\n'
                  'def score(value):\n    result=[item for item in pieces(value)]\n'
                  '    assert isinstance(result,list)\n    return result\n')
        for change in ('binding', 'default'):
            with self.subTest(change=change):
                args = {'value': ['a']}
                binding, entry, unit = self.prepared(source, args)
                receipt = binding.import_only_entries[unit['id']]
                namespace = receipt['namespace']
                helper = namespace['pieces']
                def mutate():
                    if change == 'binding':
                        namespace['pieces'] = lambda value, n=2: iter([n])
                    else:
                        helper.__defaults__ = (3,)
                self.invoke_after_load_mutation(binding, entry, unit, args, mutate)

    def test_omitted_entry_default_is_rechecked_before_and_after_load(self):
        source = 'def score(value, option=None):\n    assert isinstance(value,list)\n    return 7\n'
        args = {'value': []}
        for when in ('before_load', 'after_load'):
            with self.subTest(when=when):
                binding, entry, unit = self.prepared(source, args)
                original = binding.import_only_entries[unit['id']]['function']
                def mutate():
                    original.__defaults__ = ('changed',)
                if when == 'after_load':
                    self.invoke_after_load_mutation(binding, entry, unit, args, mutate)
                else:
                    mutate()
                    with patch.object(binding, '_load', side_effect=AssertionError('load was reached')) as load:
                        with self.assertRaises(ValueError):
                            binding.invoke(entry, unit, args)
                    load.assert_not_called()


if __name__ == '__main__':
    unittest.main()
