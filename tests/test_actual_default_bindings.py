"""Native binding inspection never consults foreign mapping-key protocols."""
import builtins
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.source.call_value_conditions import CallUnknown, actual_defaults, builtin_state


class ActualDefaultBindingTests(unittest.TestCase):
    def test_builtin_namespace_subclass_key_rejects_before_any_lookup_hook(self):
        self.assertTrue(builtin_state())
        touched = []
        class Key(str):
            __hash__ = str.__hash__
            def __eq__(self, other):
                touched.append('eq')
                return str.__eq__(self, other)
        namespace = builtins.__dict__
        original = namespace.pop('isinstance')
        key = Key('isinstance')
        namespace[key] = original
        touched.clear()
        try:
            checked = builtin_state()
        finally:
            namespace.pop(key)
            namespace['isinstance'] = original
        self.assertIs(checked, False)
        self.assertEqual(touched, [])
        self.assertTrue(builtin_state())

    def test_native_positional_keyword_and_wrapped_defaults(self):
        def original(value=3, *, option=None): pass
        def wrapped(*args, **kwargs): pass
        wrapped.__wrapped__ = original
        self.assertEqual(actual_defaults(wrapped, ['value']), dict(value=3, option=None))

    def test_metadata_and_keyword_default_subclass_keys_are_rejected_before_lookup(self):
        touched = []
        class Key(str):
            def __hash__(self): touched.append('hash'); return super().__hash__()
            def __eq__(self, other): touched.append('eq'); return super().__eq__(other)
        def original(value=3, *, option=None): pass
        key = Key('__wrapped__')
        original.__dict__[key] = None
        touched.clear()
        with self.assertRaises(CallUnknown): actual_defaults(original, ['value'])
        self.assertEqual(touched, [])
        original.__dict__.clear()
        original.__kwdefaults__ = {Key('option'): None}
        touched.clear()
        with self.assertRaises(CallUnknown): actual_defaults(original, ['value'])
        self.assertEqual(touched, [])

    def test_parameter_name_subclasses_are_not_used_as_dictionary_keys(self):
        touched = []
        class Key(str):
            def __hash__(self): touched.append('hash'); return super().__hash__()
            def __eq__(self, other): touched.append('eq'); return super().__eq__(other)
        def original(value=3): pass
        with self.assertRaises(CallUnknown): actual_defaults(original, [Key('value')])
        self.assertEqual(touched, [])

    def test_default_container_subclasses_are_not_truth_tested(self):
        touched = []
        class Keywords(dict):
            def __len__(self): touched.append('len'); return 1
        class Defaults(tuple):
            def __len__(self): touched.append('len'); return 1
        def original(value=3): pass
        original.__kwdefaults__ = Keywords(option=None)
        with self.assertRaises(CallUnknown): actual_defaults(original, ['value'])
        self.assertEqual(touched, [])
        original.__kwdefaults__ = None
        original.__defaults__ = Defaults((3,))
        with self.assertRaises(CallUnknown): actual_defaults(original, ['value'])
        self.assertEqual(touched, [])


if __name__ == '__main__':
    unittest.main()
