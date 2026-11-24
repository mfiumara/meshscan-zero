# SPDX-License-Identifier: GPL-3.0-or-later
"""Small device-side regression tests; no access to the real meshscan service."""
import os
import http.server
import json
import threading
import time
import unittest
import tempfile
from pathlib import Path
from PIL import Image
import map_tiles
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

    def test_projection_fit_and_age_filter(self):
        lat, lon = 52.37, 4.89
        x, y = map_tiles.project(lat, lon, 10)
        actual_lat, actual_lon = map_tiles.unproject(x, y, 10)
        self.assertAlmostEqual(lat, actual_lat)
        self.assertAlmostEqual(lon, actual_lon)
        now = time.time()
        nodes = [{'lat':lat,'lon':lon,'last_seen':now}, {'lat':52.5,'lon':5.1,'last_seen':now-90000}, {'node_id':'no-gps','last_seen':now}]
        self.assertEqual(len(radar.visible_nodes(nodes, False, now)), 1)
        self.assertEqual(len(radar.visible_nodes(nodes, True, now)), 2)
        self.assertIsNotNone(map_tiles.fit(nodes))
        self.assertIsNone(map_tiles.fit([{'node_id':'no-gps'}]))

    def test_home_does_not_preempt_initial_fit(self):
        data = radar.Data.__new__(radar.Data)
        data.lock = threading.Lock()
        data.status, data.summary, data.nodes, data.hops, data.messages, data.settings = {}, {}, [], [], [], {}
        data.online, data.events = False, []
        with patch.dict(os.environ, {'RADAR_HOME':'52.37,4.89', 'MESH_MAP_OFFLINE':'1'}):
            renderer = radar.Renderer()
            renderer.render(data, time.monotonic(), False, 0, 'http://localhost:8080')
            self.assertIsNone(renderer.view)
            data.nodes = [{'lat':52.5,'lon':5.0,'node_id':'a','last_seen':time.time()}]
            data.online = True
            renderer.render(data, time.monotonic(), False, 0, 'http://localhost:8080')
            self.assertEqual(renderer.view, map_tiles.fit(data.nodes))

    def test_cardputer_zoom_keys(self):
        renderer = radar.Renderer()
        renderer.view = (1000, 1000, 6)
        renderer.change_view(72)  # Sym+'+'
        self.assertEqual(renderer.view, (2000, 2000, 7))
        renderer.change_view(71)  # Sym+'-'
        self.assertEqual(renderer.view, (1000, 1000, 6))
        renderer.change_view(106)  # right arrow
        self.assertEqual(renderer.view, (1040, 1000, 6))
        renderer.home = {'lat': 52.37, 'lon': 4.89}
        renderer.change_view(35)  # H
        self.assertEqual(renderer.view, (*map_tiles.project(52.37, 4.89, 6), 6))
        renderer.nodes = [{'lat':52.5,'lon':5.0,'node_id':'a','last_seen':time.time()}]
        renderer.change_view(33)  # F
        self.assertEqual(renderer.view, map_tiles.fit(renderer.nodes))

    def test_offline_cached_tile_and_missing_tile(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'MESH_MAP_OFFLINE':'1'}):
            worker = map_tiles.TileWorker(tmp)
            tile = Path(tmp)/'2'/'2'/'1.png'
            tile.parent.mkdir(parents=True)
            Image.new('RGB', (256,256), (5,55,80)).save(tile)
            self.assertEqual(worker.tile(2,2,1).getpixel((10,10)), (5,55,80))
            image, missing = worker.make_view((600,400,2))
            self.assertGreater(missing, 0)
            self.assertEqual(image.size, (190,170))

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
