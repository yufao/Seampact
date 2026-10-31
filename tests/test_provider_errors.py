import http.client
import io
import json
import socket
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.providers import ProviderError, Transport


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


if __name__ == '__main__':
    unittest.main()
