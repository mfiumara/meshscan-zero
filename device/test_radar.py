# SPDX-License-Identifier: GPL-3.0-or-later
"""Small device-side regression tests; no access to the real meshscan service."""
import os
import http.server
import json
import threading
import time
import unittest
from unittest.mock import patch

import radar


class RadarTests(unittest.TestCase):
    def test_home_override_and_presets(self):
        with patch.dict(os.environ, {'RADAR_HOME': '52.37,4.89'}):
            self.assertEqual(radar.home_override(), {'lat': 52.37, 'lon': 4.89})
        with patch.dict(os.environ, {'RADAR_HOME': '91,4'}):
            self.assertIsNone(radar.home_override())
        self.assertEqual(radar.preset_name('meshcore_nl_sf7'), 'MeshCore NL')
        self.assertEqual(radar.preset_name('meshtastic_longfast'), 'Meshtastic LongFast')
        self.assertNotIn('_', radar.preset_name('meshcore_future_sf12'))
        self.assertEqual(radar.clip('Mesh 🇳🇱 node', 100, radar.FONT), 'Mesh  node')

    def test_position(self):
        node = {'node_id': 'abc', 'last_rssi': -80}
        self.assertEqual(radar.position(node, None), radar.position(node, None))
        near = radar.position(node, None)
        far = radar.position(dict(node, last_rssi=-120), None)
        self.assertLess((near[0]-radar.CX)**2 + (near[1]-radar.CY)**2, (far[0]-radar.CX)**2 + (far[1]-radar.CY)**2)
        north = radar.position(dict(node, lat=52.1, lon=5), {'lat': 52, 'lon': 5})
        self.assertAlmostEqual(north[0], radar.CX, places=5)
        self.assertLess(north[1], 85)

    def test_event_and_empty_render(self):
        data = radar.Data.__new__(radar.Data)
        data.lock = threading.Lock()
        data.status, data.summary, data.nodes, data.hops, data.messages, data.settings = {}, {}, [], [], [], {}
        data.online, data.events, data.ticker = False, [], ''
        data.on_event('packet', {'decoded': json.dumps({'type': 'GRP_TXT', 'path': ['a1b2']})})
        self.assertEqual(data.events[-1][1], ['a1b2'])
        self.assertEqual(data.ticker, 'GRP TXT')
        self.assertEqual(radar.render(data, time.monotonic(), False, 0, 'http://127.0.0.1:8080').size, (320, 170))
        self.assertEqual(radar.render(data, time.monotonic(), True, 0, 'http://127.0.0.1:8080').size, (320, 170))

    def test_poll_recovers_from_outage_and_missing_settings(self):
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path in ('/api/settings', '/api/stream'):
                    self.send_error(404)
                    return
                payload = {'/api/status': {}, '/api/summary': {}, '/api/nodes': [], '/api/hops': [], '/api/messages': []}.get(self.path)
                if payload is None:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps(payload).encode())
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        # Start without a serving thread to simulate a down API, then bring it up.
        with patch.object(radar, 'BASE', 'http://127.0.0.1:%d' % server.server_port):
            data = radar.Data()
            time.sleep(.2)
            self.assertFalse(data.online)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            deadline = time.monotonic() + 10
            while not data.online and time.monotonic() < deadline:
                time.sleep(.1)
            self.assertTrue(data.online)
            self.assertEqual(data.settings, {})
            server.shutdown()
            server.server_close()
            deadline = time.monotonic() + 10
            while data.online and time.monotonic() < deadline:
                time.sleep(.1)
            self.assertFalse(data.online)


if __name__ == '__main__':
    unittest.main()
