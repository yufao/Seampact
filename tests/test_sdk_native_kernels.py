"""Read-only platform kernel checks preserve original progress side effects."""
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from seampact.source.sdk_native_kernels import capture_native_kernels,verify_native_kernels,verify_read_kernels


class ProgressNativeKernelTests(unittest.TestCase):
    def test_current_thread_clock_and_stream_checks_do_not_create_work(self):
        before=tuple(threading.enumerate())
        result=verify_native_kernels(capture_native_kernels())
        self.assertEqual(result['guards']['thread_clock_atexit']['status'],'CHECKED')
        self.assertEqual(tuple(threading.enumerate()),before)
        self.assertEqual(result['lifecycle_permission'],'NOT_GRANTED')
        self.assertIn(result['status'],('CHECKED','UNKNOWN'))

    def test_changed_clock_and_original_thread_method_fail_before_use(self):
        context=capture_native_kernels()
        with patch.object(time,'time',lambda:0):
            self.assertFalse(verify_native_kernels(context)['allowed'])
        with patch.object(threading.Thread,'start',lambda self:None):
            self.assertFalse(verify_native_kernels(context)['allowed'])

    def test_metadata_object_cannot_supply_native_kernel_provenance(self):
        result=verify_native_kernels(dict(status='CHECKED',allowed=True))
        self.assertFalse(result['allowed'])
        self.assertIn('occurrence',result['remaining'][0])

    def test_original_utf8_and_native_reader_definitions_are_checked_without_opening_files(self):
        context=capture_native_kernels()
        self.assertEqual(verify_read_kernels(context)['status'],'CHECKED')
        import codecs
        with patch.object(codecs,'getdecoder',lambda name:None):
            self.assertFalse(verify_read_kernels(context)['allowed'])


if __name__=='__main__':
    unittest.main()
