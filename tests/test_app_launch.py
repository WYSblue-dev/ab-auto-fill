"""Launcher lifecycle tests never open a browser or contact a real server."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import requests

import local_app


class AppLaunchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.server = SimpleNamespace(origin='http://127.0.0.1:8765', token='a' * 43,
                                      instance_id='b' * 32,
                                      installation_id=local_app._installation_id(self.root))
        self.path = self.root / local_app.SESSION_FILE
        self.identity = {'instance_id': self.server.instance_id,
                         'installation_id': self.server.installation_id}
        self.session = Mock()
        self.session.get.return_value = Mock(status_code=200)
        self.session.get.return_value.json.return_value = self.identity
        factory = patch.object(local_app.requests, 'Session')
        self.factory = factory.start()
        self.addCleanup(factory.stop)
        self.factory.return_value.__enter__.return_value = self.session
        browser = patch.object(local_app.webbrowser, 'open', return_value=True)
        self.browser = browser.start()
        self.addCleanup(browser.stop)

    def save(self, **updates):
        local_app._write_session(self.root, self.server)
        if updates:
            data = json.loads(self.path.read_text())
            data.update(updates)
            self.path.write_text(json.dumps(data))

    def main(self, *extra):
        output = io.StringIO()
        with patch('sys.argv', ['local_app.py', '--data-dir', str(self.root), *extra]), redirect_stdout(output):
            result = local_app.main()
        return result, output.getvalue()

    def test_running_same_installation_reopens_without_second_server_or_token_log(self):
        self.save()
        with patch.object(local_app, 'LocalServer', side_effect=OSError('occupied')) as server:
            result, output = self.main()
        self.assertEqual(result, 0)
        server.assert_called_once()
        self.browser.assert_called_once_with(f'{self.server.origin}/#{self.server.token}')
        self.assertNotIn(self.server.token, output)
        self.assertFalse(self.session.trust_env)
        self.session.get.assert_called_once_with(
            self.server.origin + '/api/session',
            headers={'X-App-Token': self.server.token, 'Accept': 'application/json'},
            timeout=(1, 2), allow_redirects=False)

    def test_no_browser_reopen_prints_authenticated_local_url_only_on_request(self):
        self.save()
        with patch.object(local_app, 'LocalServer', side_effect=OSError('occupied')):
            result, output = self.main('--no-browser')
        self.assertEqual(result, 0)
        self.assertIn(f'{self.server.origin}/#{self.server.token}', output)
        self.browser.assert_not_called()

    def test_untrusted_descriptor_never_sends_token_or_opens_browser(self):
        for updates in ({'origin': 'https://evil.example'}, {'origin': 'http://127.0.0.1:9000'},
                        {'origin': 'http://localhost:8765'}, {'installation_id': 'c' * 64},
                        {'instance_id': 'invalid'}, {'token': 'bad token'}, {'version': True},
                        {'extra': 'unexpected'}):
            with self.subTest(updates=updates):
                self.save(**updates)
                self.assertIsNone(local_app._running_session_url(self.root, 8765))
        self.path.write_text('{not json')
        self.assertIsNone(local_app._running_session_url(self.root, 8765))
        self.path.unlink()
        self.assertIsNone(local_app._running_session_url(self.root, 8765))
        self.session.get.assert_not_called()
        self.browser.assert_not_called()

    def test_wrong_server_stale_token_redirect_and_network_failure_are_rejected(self):
        self.save()
        for status, document in ((403, self.identity), (302, self.identity),
                                 (200, {**self.identity, 'instance_id': 'd' * 32}),
                                 (200, {**self.identity, 'installation_id': 'd' * 64}),
                                 (200, {'unrelated': 'server'})):
            with self.subTest(status=status, document=document):
                self.session.get.return_value = Mock(status_code=status)
                self.session.get.return_value.json.return_value = document
                self.assertIsNone(local_app._running_session_url(self.root, 8765))
        self.session.get.side_effect = requests.Timeout('not logged')
        with patch.object(local_app, 'LocalServer', side_effect=OSError('occupied')):
            result, output = self.main()
        self.assertEqual(result, 1)
        self.assertNotIn(self.server.token, output)
        self.assertNotIn('not logged', output)
        self.browser.assert_not_called()

    def test_session_file_is_private_and_cleanup_preserves_newer_instance(self):
        self.save()
        if os.name != 'nt':
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        local_app._remove_session(self.root, self.server)
        self.assertFalse(self.path.exists())
        self.save(instance_id='d' * 32, token='e' * 43)
        local_app._remove_session(self.root, self.server)
        self.assertEqual(json.loads(self.path.read_text())['instance_id'], 'd' * 32)

    def test_fresh_launch_publishes_before_open_and_cleans_before_releasing_port(self):
        server = Mock(**vars(self.server))
        def serve():
            self.assertEqual(local_app._read_session(self.root), local_app._session_descriptor(server))
            self.browser.assert_called_once()
        server.serve_forever.side_effect = serve
        server.server_close.side_effect = lambda: self.assertFalse(self.path.exists())
        with patch.object(local_app, 'LocalServer', return_value=server):
            result, output = self.main()
        self.assertEqual(result, 0)
        self.assertNotIn(self.server.token, output)
        self.assertFalse(self.path.exists())

    def test_session_endpoint_requires_token_and_never_returns_it(self):
        handler = object.__new__(local_app.Handler)
        handler.path = '/api/session'
        handler.server = self.server
        handler.headers = {'Host': '127.0.0.1:8765', 'X-App-Token': ''}
        handler.reply = Mock()
        handler.do_GET()
        self.assertEqual(handler.reply.call_args.args[0], 403)
        handler.headers['X-App-Token'] = self.server.token
        handler.do_GET()
        handler.reply.assert_called_with(200, self.identity)
        app = local_app.Application(self.root, self.root / 'composed_info')
        self.assertNotIn(self.server.token, json.dumps(app.state()))


if __name__ == '__main__':
    unittest.main()
