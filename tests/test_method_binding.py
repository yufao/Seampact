import asyncio
import functools
import inspect
import tempfile
import unittest
from pathlib import Path

from seampact.handoff import install_method_proxy
from seampact.runtime import Coordinator, OwnerRuntime, ProtocolError
from seampact.store import Store


class NativeOwner:
    def __init__(self):
        self.entries = []

    def synchronous(self, value: int, *, option=False):
        self.entries.append((value, option))
        return self.entries

    async def asynchronous(self, value: int):
        self.entries.append(value)
        return self.entries

NativeOwner.asynchronous._step_config = {"native": "metadata"}


class MethodBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "ledger.db")
        self.runtime = OwnerRuntime(Coordinator(self.store))
        self.owner = NativeOwner()

    def tearDown(self):
        self.runtime.close()
        self.store.close()
        self.temporary.cleanup()

    def test_sdk_method_discovery_signature_metadata_and_single_self(self):
        before = self.owner.asynchronous
        proxy = self.runtime.wrap(before, "async", "owner")
        self.runtime.call("register", "async", "owner")
        attached = install_method_proxy(self.owner, "asynchronous", proxy)
        sdk_methods = dict(inspect.getmembers(self.owner, predicate=inspect.ismethod))
        self.assertIn("asynchronous", sdk_methods)
        self.assertEqual(inspect.signature(attached), inspect.signature(before))
        self.assertIs(attached._step_config, before._step_config)
        @functools.wraps(attached)
        async def sdk_dispatch(*args, **kwargs):
            return await attached(*args, **kwargs)
        self.assertEqual(inspect.signature(sdk_dispatch), inspect.signature(before))
        result = asyncio.run(self.owner.asynchronous(4))
        self.assertIs(result, self.owner.entries)
        self.assertEqual(self.owner.entries, [4])
        call = next(iter(self.runtime.snapshot()["calls"].values()))
        self.assertEqual((call["starts"], call["physical"]), (1, "RETIRED"))

    def test_sync_receiver_and_keyword_only_arguments_are_preserved(self):
        before = self.owner.synchronous
        proxy = self.runtime.wrap(before, "sync", "owner")
        self.runtime.call("register", "sync", "owner")
        attached = install_method_proxy(self.owner, "synchronous", proxy)
        self.assertEqual(inspect.signature(before), inspect.signature(attached))
        self.assertIs(self.owner.synchronous(3, option=True), self.owner.entries)
        self.assertEqual(self.owner.entries, [(3, True)])
        with self.assertRaises(ProtocolError):
            attached.__func__(NativeOwner(), 8)
        self.assertEqual(self.owner.entries, [(3, True)])

    def test_wrong_owner_member_and_double_install_are_rejected(self):
        proxy = self.runtime.wrap(self.owner.asynchronous, "async", "owner")
        with self.assertRaises(ProtocolError):
            install_method_proxy(NativeOwner(), "asynchronous", proxy)
        with self.assertRaises(ProtocolError):
            install_method_proxy(self.owner, "synchronous", proxy)
        install_method_proxy(self.owner, "asynchronous", proxy)
        with self.assertRaises(ProtocolError):
            install_method_proxy(self.owner, "asynchronous", proxy)


if __name__ == "__main__":
    unittest.main()
