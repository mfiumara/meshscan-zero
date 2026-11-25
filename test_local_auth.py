# SPDX-License-Identifier: GPL-3.0-or-later
"""Local-control authentication regression tests (no radio or real state)."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('meshscan_web', Path(__file__).parent/'meshscan-web.py')
web = importlib.util.module_from_spec(spec)
spec.loader.exec_module(web)


class LocalAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ctl_patch = patch.object(web.common, 'CTL', self.tmp.name)
        self.ctl_patch.start()
        self.settings_patch = patch.object(web.common, 'load_settings', return_value={'name':'test'})
        self.settings_patch.start()
        self.status_patch = patch.object(web, 'status', return_value={'mode':'chat'})
        self.status_patch.start()
        web.LOCAL_TOKEN = web.init_local_token()
        self.server = web.Server(('127.0.0.1', 0), web.H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = 'http://127.0.0.1:%d' % self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.status_patch.stop()
        self.settings_patch.stop()
        self.ctl_patch.stop()
        self.tmp.cleanup()

    def post(self, path, body, headers=None):
        req = Request(self.base+path, json.dumps(body).encode(), headers={'Content-Type':'application/json', **(headers or {})})
        try:
            with urlopen(req, timeout=3) as r:
                return r.status
        except HTTPError as e:
            return e.code

    def test_token_is_private_and_stable(self):
        p = Path(self.tmp.name)/'local.token'
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual(web.init_local_token(), web.LOCAL_TOKEN)
        os.chmod(p, 0o644)
        with self.assertRaises(PermissionError):
            web.init_local_token()

    def test_token_rejects_symlink(self):
        token = Path(self.tmp.name)/'local.token'
        token.unlink()
        target = Path(self.tmp.name)/'elsewhere'
        target.write_text('A'*43)
        os.chmod(target, 0o600)
        token.symlink_to(target)
        with self.assertRaises(OSError):
            web.init_local_token()

    def test_local_send_mode_and_restrictions(self):
        h = {web.LOCAL_HEADER:web.LOCAL_TOKEN}
        self.assertEqual(self.post('/api/send', {'text':'private', 'channel':1}, h), 202)
        self.assertEqual(self.post('/api/mode', {'mode':'scan'}, h), 200)
        self.assertEqual(self.post('/api/settings', {}, h), 401)
        self.assertEqual(self.post('/api/send', {'text':'x'}, {}), 401)
        self.assertEqual(self.post('/api/send', {'text':'x'}, {web.LOCAL_HEADER:'bad'}), 401)
        self.assertEqual(self.post('/api/send', {'text':'x'}, {**h, 'Origin':'http://evil.example'}), 401)
        self.assertEqual(self.post('/api/send', {'text':'x'}, {**h, 'X-Forwarded-For':'8.8.8.8'}), 401)
        self.assertEqual(self.post('/api/send', {'text':'x'*141, 'channel':1}, h), 400)
        with patch.object(web.common, 'load_settings', return_value=None):
            self.assertEqual(self.post('/api/mode', {'mode':'chat'}, h), 409)
        self.assertFalse(web.valid_local_request('192.168.1.1', h, '/api/send'))
        self.assertFalse(web.valid_local_request('::ffff:127.0.0.1', h, '/api/send'))
        self.assertTrue(web.valid_local_request('::1', h, '/api/send'))
        with self.assertRaises(HTTPError) as e:
            urlopen(self.base+'/local.token')
        self.assertEqual(e.exception.code, 404)

    def test_radio_control_auth_and_file(self):
        h = {web.LOCAL_HEADER:web.LOCAL_TOKEN}
        self.assertTrue(web.common.radio_on())
        self.assertEqual(self.post('/api/radio', {'on':False}, h), 200)
        self.assertFalse(web.common.radio_on())
        self.assertEqual(self.post('/api/send', {'text':'x'}, h), 409)
        self.assertEqual(self.post('/api/radio', {'on':'yes'}, h), 400)
        self.assertEqual(self.post('/api/radio', {'on':True}, {**h, 'Origin':'http://evil.example'}), 401)
        self.assertFalse(web.valid_local_request('192.168.1.1', h, '/api/radio'))
        session = web.new_session()
        self.assertEqual(self.post('/api/radio', {'on':True}, {'Cookie':web.COOKIE+'='+session}), 200)
        self.assertTrue(web.common.radio_on())

    def test_browser_session_still_works(self):
        session = web.new_session()
        self.assertEqual(self.post('/api/mode', {'mode':'scan'}, {'Cookie':web.COOKIE+'='+session}), 200)


if __name__ == '__main__':
    unittest.main()
