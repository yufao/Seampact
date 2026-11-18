"""Conditional model-role switches cannot overwrite an intervening user change."""
import copy
import json
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from seampact.providers import Profiles
from seampact.server import make_server


class ModelSelectionCASTests(unittest.TestCase):
    def setUp(self):
        self.profiles = Profiles.__new__(Profiles)
        self.profiles.lock = threading.RLock()
        self.profiles.roles = dict(chat='first', analysis='first', teacher='first')
        self.profiles.presets = {}
        self.profiles.connections = {name: dict(id=name, protocol='openai-compatible',
            base_url='https://example.invalid', model='fixture-model', parameters={}, auth=dict(mode='none'),
            config_digest='config-' + name, revision=1) for name in ('first', 'second')}
        self.profiles._save = Mock()
        self.profiles._public = self.profiles.role_profile
        self.expected = dict(connection_id='first', config_digest='config-first', revision=1)

    def test_matching_identity_switches_and_legacy_unconditional_selection_still_works(self):
        selected = self.profiles.select_role('chat', 'second', expected_current=self.expected)
        self.assertEqual(selected['connection_id'], 'second')
        self.profiles._save.assert_called_once_with()
        self.profiles.select_role('chat', 'first')
        self.assertEqual(self.profiles.roles['chat'], 'first')

    def test_changed_selection_config_or_revision_rejects_without_save(self):
        for field, value in (('connection_id', 'second'), ('config_digest', 'edited'), ('revision', 2)):
            with self.subTest(field=field):
                expected = dict(self.expected, **{field: value})
                before = copy.deepcopy(self.profiles.roles)
                with self.assertRaisesRegex(ValueError, '已被修改'):
                    self.profiles.select_role('chat', 'second', expected_current=expected)
                self.assertEqual(self.profiles.roles, before)
                self.profiles._save.assert_not_called()

    def test_partial_or_malformed_identity_never_becomes_unconditional_selection(self):
        for expected in ({}, dict(connection_id='first'), dict(self.expected, revision=True),
                         dict(self.expected, revision=-1), dict(self.expected, extra='ignored'), 'first'):
            with self.subTest(expected=expected), self.assertRaisesRegex(ValueError, 'expected_current'):
                self.profiles.select_role('chat', 'second', expected_current=expected)
        self.assertEqual(self.profiles.roles['chat'], 'first')
        self.profiles._save.assert_not_called()

    def test_waiting_restore_observes_intervening_selection_inside_the_same_lock(self):
        entered, errors = threading.Event(), []
        def restore():
            entered.set()
            try:
                self.profiles.select_role('chat', 'first', expected_current=self.expected)
            except ValueError as error:
                errors.append(str(error))
        with self.profiles.lock:
            thread = threading.Thread(target=restore)
            thread.start()
            self.assertTrue(entered.wait(2))
            self.profiles.select_role('chat', 'second')
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.profiles.roles['chat'], 'second')
        self.profiles._save.assert_called_once_with()

    def test_http_endpoint_passes_cas_and_rejects_stale_restoration(self):
        server = make_server(SimpleNamespace(profiles=self.profiles), port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = 'http://127.0.0.1:' + str(server.server_port) + '/api/models/select'
            def request(body):
                with urlopen(Request(url, data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'}), timeout=3) as response:
                    return json.load(response)
            result = request(dict(role='chat', connection_id='second', expected_current=self.expected))
            self.assertEqual(result['connection_id'], 'second')
            with self.assertRaises(HTTPError) as rejected:
                request(dict(role='chat', connection_id='first', expected_current=self.expected))
            self.assertEqual(rejected.exception.code, 400)
            self.assertEqual(self.profiles.roles['chat'], 'second')
            request(dict(role='chat', connection_id='first'))
            self.assertEqual(self.profiles.roles['chat'], 'first')
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)


if __name__ == '__main__':
    unittest.main()
