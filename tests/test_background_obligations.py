"""Real Python work must end before a source-qualified owner can be reclaimed."""
from contextlib import contextmanager
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from seampact.hosting.background import GUARD,SCHEMA,census,quiescent,required
from seampact.hosting.deployment import Deployments
from seampact.runtime import ProtocolError
from seampact.store import Store
from seampact.worker import Worker,make_worker_server


def contract(status='SUPPORTED'):
    return dict(lifecycle=dict(checks=dict(cleanup=dict(status=status,guards=[GUARD]))))


class BackgroundObligationTests(unittest.TestCase):
    @contextmanager
    def background(self):
        done=threading.Event()
        thread=threading.Thread(target=done.wait,name='seampact-trusted-looking-name')
        thread.start()
        try:yield thread
        finally:done.set();thread.join(5)

    def worker(self):
        w=Worker.__new__(Worker);w.lock=threading.Lock()
        w.host_threads={id(thread):thread for thread in threading.enumerate()}
        w.catalog=SimpleNamespace(bindings={});w.group=dict(contract=contract())
        w._identify=lambda body:None;w.identity=lambda:{}
        w.accepting=False;w.in_flight=0;w.server=None
        return w

    def test_live_thread_is_pending_regardless_of_its_name(self):
        known=threading.enumerate()
        with self.background():
            report=census(known)
            self.assertFalse(quiescent(report));self.assertGreater(report['untracked_count'],0)
            self.assertIn('CUDA',report['not_checked'])
        self.assertTrue(quiescent(census(known)))

    def test_empty_name_and_boolean_count_cannot_claim_quiescence(self):
        for report in [None,{},dict(schema=SCHEMA,status='QUIESCENT',untracked_count=False),
                       dict(schema=SCHEMA,status='QUIESCENT',untracked_count=1),
                       dict(schema='other',status='QUIESCENT',untracked_count=0)]:
            self.assertFalse(quiescent(report))

    def test_unknown_source_does_not_acquire_or_require_a_granted_guard(self):
        self.assertFalse(required(contract('UNKNOWN')))
        self.assertTrue(required(contract()))
        self.assertFalse(required({}))

    def test_shutdown_waits_for_work_after_the_original_call_counter_is_zero(self):
        worker=self.worker()
        with self.background():
            with self.assertRaisesRegex(ProtocolError,'后台线程'):
                worker.shutdown({})
        self.assertTrue(worker.shutdown({})['stopping'])

    def test_actual_http_host_threads_are_registered_by_identity(self):
        worker=self.worker();worker.health=worker._python_threads
        server=make_worker_server(worker,'127.0.0.1',0)
        def serve():
            with worker.hosting_thread():server.serve_forever(poll_interval=.02)
        thread=threading.Thread(target=serve);thread.start()
        try:
            with urlopen('http://127.0.0.1:'+str(server.server_port)+'/health',timeout=3) as response:
                self.assertTrue(quiescent(json.load(response)))
        finally:server.shutdown();thread.join(5);server.server_close()

    def test_controller_requires_a_matching_zero_work_receipt(self):
        store=Store(':memory:');self.addCleanup(store.close)
        store.save('runtime_group','group',dict(contract=contract()))
        d=Deployments.__new__(Deployments);d.affinity={}
        d.platform=SimpleNamespace(store=store,runtime=SimpleNamespace(call=lambda method:dict(roots={},calls={})))
        d._check_identity=lambda record,health:None
        record=dict(id='instance',group_id='group',state='DRAINING',in_flight=0,endpoint='unused',pending_stop={})
        for receipt in [None,dict(schema=SCHEMA,status='PENDING',untracked_count=1)]:
            with patch('seampact.hosting.deployment.json_request',return_value=dict(in_flight=0,python_threads=receipt)):
                self.assertFalse(d._drained(record))
        with patch('seampact.hosting.deployment.json_request',return_value=dict(in_flight=0,
            python_threads=dict(schema=SCHEMA,status='QUIESCENT',untracked_count=0))):
            self.assertTrue(d._drained(record));self.assertNotIn('drain_blocked',record)


if __name__=='__main__':unittest.main()
