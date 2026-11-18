import http.client
import io
import json
import socket
import sys
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.providers import ProviderError, RequestCancelled, RequestControl, Transport


class ProviderErrorChecks(unittest.TestCase):
    def http_error(self, status, body):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        error = urllib.error.HTTPError('https://example.invalid', status, 'secret', {}, io.BytesIO(raw))
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = error
            with self.assertRaises(ProviderError) as result:
                Transport().send('https://example.invalid', 'private-key', {'input': 'source-code'})
        return result.exception

    def test_recognized_errors(self):
        cases = [
            (400, {'error': {'code': 'context_length_exceeded', 'max_context_tokens': 128000}}, 'context_length'),
            (400, {'error': {'message': 'Request body is too large'}}, 'request_too_large'),
            (400, {'error': {'message': 'max_tokens exceeds the maximum allowed'}}, 'output_limit'),
            (413, b'large', 'request_too_large'),
            (401, b'', 'authentication'), (429, b'', 'rate_limit'), (503, b'', 'server'),
        ]
        for status, body, category in cases:
            with self.subTest(category=category):
                error = self.http_error(status, body)
                self.assertEqual(error.kind, category)
                self.assertEqual(error.diagnostic['category'], category)
                self.assertEqual(error.diagnostic['http_status'], status)
                self.assertIn(str(status), str(error))
        self.assertEqual(self.http_error(400, cases[0][1]).diagnostic['max_context_tokens'], 128000)

    def test_unrecognized_and_bounded_body(self):
        for body in ({'error': {'code': 'other'}}, b'not json', b'x' * 20000):
            error = self.http_error(400, body)
            self.assertEqual(error.kind, 'invalid_request')
            self.assertEqual(error.diagnostic, {'http_status': 400, 'category': 'invalid_request'})

    def test_sensitive_response_is_never_retained(self):
        secret = 'private-key source-code https://private.invalid'
        error = self.http_error(400, {'error': {'message': 'context length exceeded ' + secret,
                                                'max_context_tokens': secret}})
        self.assertEqual(error.kind, 'context_length')
        self.assertNotIn(secret, str(error))
        self.assertNotIn(secret, repr(error.diagnostic))
        self.assertNotIn(secret, repr(error.__dict__))
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)

    def test_transport_semantics(self):
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = urllib.error.URLError(socket.gaierror('failed'))
            with self.assertRaises(ProviderError) as result:
                Transport().send('https://example.invalid')
        self.assertEqual(result.exception.kind, 'transport')
        self.assertIsNone(result.exception.diagnostic)
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = http.client.IncompleteRead(b'partial')
            with self.assertRaises(ProviderError) as result:
                Transport().send('https://example.invalid')
        self.assertEqual(result.exception.kind, 'response_incomplete')
        self.assertIsNone(result.exception.diagnostic)

    def test_task_deadline_sets_remaining_timeout_and_legacy_default(self):
        from types import SimpleNamespace
        clock = [100.0]
        with patch('seampact.providers.time', SimpleNamespace(monotonic=lambda: clock[0])):
            control = RequestControl(deadline_at=1000.0)
            with patch('urllib.request.build_opener') as opener:
                opener.return_value.open.side_effect = lambda *a, **kw: io.BytesIO(b'{}')
                Transport().send('https://example.invalid', control=control)
                self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], 900)
                clock[0] = 350.0
                Transport().send('https://example.invalid', control=control)
                self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], 650)
                Transport().send('https://example.invalid')
                self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], 600)
                clock[0] = 1000.0
                with self.assertRaises(RequestCancelled):
                    Transport().send('https://example.invalid', control=control)
                self.assertEqual(opener.return_value.open.call_count, 3)

    def test_read_error_after_cancellation_is_not_a_transport_failure(self):
        for error in (AttributeError('closed'), OSError('closed')):
            with self.subTest(error=type(error).__name__):
                control = RequestControl()
                class ClosedResponse(io.BytesIO):
                    def readline(self, *args):
                        control.cancelled.set()
                        raise error
                with patch('urllib.request.build_opener') as opener:
                    opener.return_value.open.return_value = ClosedResponse()
                    with self.assertRaises(RequestCancelled):
                        Transport().stream('https://example.invalid', '', {}, lambda value: None, control=control)
                    self.assertEqual(opener.return_value.open.call_count, 1)

        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = AttributeError('other')
            with self.assertRaises(ProviderError) as result:
                Transport().send('https://example.invalid', control=RequestControl())
            self.assertNotIsInstance(result.exception, RequestCancelled)
            self.assertEqual(result.exception.kind, 'invalid_response')

    def test_explicit_unlimited_has_no_timeout_and_legacy_control_keeps_600(self):
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = lambda *a, **kw: io.BytesIO(b'{}')
            unlimited = RequestControl(unlimited=True)
            Transport().send('http://example.invalid',control=unlimited)
            self.assertIsNone(opener.return_value.open.call_args.kwargs['timeout'])
            request = opener.return_value.open.call_args.args[0]
            self.assertEqual(request.get_header('X-request-id'),unlimited.request_id)
            Transport().send('http://example.invalid',control=RequestControl())
            self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'],600)

    def test_cancel_interrupts_unlimited_transport_before_headers_and_mid_stream(self):
        for send_headers in (False, True):
            with self.subTest(send_headers=send_headers):
                waiting, release = threading.Event(), threading.Event()
                control = RequestControl(unlimited=True)
                outcome = []

                class Handler(BaseHTTPRequestHandler):
                    def do_POST(self):
                        self.rfile.read(int(self.headers['Content-Length']))
                        if send_headers:
                            self.send_response(200)
                            self.send_header('Content-Type', 'text/event-stream')
                            self.end_headers()
                            self.wfile.write(b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n')
                            self.wfile.flush()
                        else:
                            waiting.set()
                        release.wait(5)

                    def log_message(self, *args):
                        pass

                with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
                    http = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
                    http.start()

                    def call():
                        try:
                            Transport().stream(f'http://127.0.0.1:{server.server_port}', '', {},
                                               lambda text: waiting.set(), control=control)
                        except Exception as error:
                            outcome.append(error)

                    request = threading.Thread(target=call, daemon=True)
                    request.start()
                    try:
                        self.assertTrue(waiting.wait(2))
                        control.cancel()
                        request.join(2)
                        self.assertFalse(request.is_alive())
                        self.assertEqual(len(outcome), 1)
                        self.assertIsInstance(outcome[0], RequestCancelled)
                    finally:
                        release.set()
                        server.shutdown()
                        http.join(2)
                        request.join(2)

    def test_partial_server_receipt_cannot_supply_complete_usage(self):
        control = RequestControl();control.begin_request()
        control.usage_receipt_url = 'http://example.invalid/seampact/usage/'+control.request_id
        receipt = dict(schema='seampact.local_model_usage.v1',request_id=control.request_id,
                       status='partial',final_usage_complete=False,
                       usage=dict(prompt_tokens=4,completion_tokens=3,total_tokens=7))
        with patch('urllib.request.build_opener') as opener:
            opener.return_value.open.side_effect = lambda *a, **kw: io.BytesIO(json.dumps(receipt).encode())
            self.assertIsNone(Transport().recover_usage(control))
            receipt.update(status='completed',final_usage_complete=True)
            self.assertEqual(Transport().recover_usage(control),receipt['usage'])
            receipt['request_id'] = 'different'
            self.assertIsNone(Transport().recover_usage(control))


if __name__ == '__main__':
    unittest.main()
