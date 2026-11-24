#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Meshscan's read-only Cardputer framebuffer map."""
import functools
import http.client
import json
import math
import mmap
import os
import select
import socket
import struct
import sys
import threading
import time
import urllib.request

import map_tiles
import numpy as np
import qrcode
from PIL import Image, ImageDraw, ImageFont

BASE = 'http://127.0.0.1:8080'
W, H = 320, 170
BLUE, GREEN, PURPLE = (94, 177, 255), (79, 220, 152), (185, 150, 255)
TEXT, MUTED = (232, 236, 242), (139, 148, 163)
FONTS = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fonts')
FONT = ImageFont.truetype(os.path.join(FONTS, 'Inter.ttf'), 10)
BOLD = ImageFont.truetype(os.path.join(FONTS, 'Inter.ttf'), 11)
BIG = ImageFont.truetype(os.path.join(FONTS, 'Inter.ttf'), 23)
MONO = ImageFont.truetype(os.path.join(FONTS, 'JetBrainsMono.ttf'), 9)
SMALL = ImageFont.truetype(os.path.join(FONTS, 'JetBrainsMono.ttf'), 7)
ATTR = ImageFont.truetype(os.path.join(FONTS, 'Inter.ttf'), 6)
for font in (BOLD, BIG):
    font.set_variation_by_name('Bold')


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=3) as response:
        return json.load(response)


def color(proto):
    return {'meshcore': BLUE, 'meshtastic': GREEN}.get(proto, PURPLE)


def dashboard_url():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(('192.0.2.1', 1))
            ip = s.getsockname()[0]
        finally:
            s.close()
        return 'http://%s:8080' % ip
    except OSError:
        return 'http://localhost:8080'


class Data:
    def __init__(self):
        self.lock = threading.Lock()
        self.status, self.summary, self.nodes, self.hops, self.messages, self.settings = {}, {}, [], [], [], {}
        self.online = False
        self.events = []
        self.ticker = 'Listening for packets'
        self.ticker_kind = 'LISTEN'
        self.ticker_sender = 'Waiting for traffic'
        threading.Thread(target=self.poll, daemon=True).start()
        threading.Thread(target=self.stream, daemon=True).start()

    def poll(self):
        while True:
            try:
                status, summary = get('/api/status'), get('/api/summary')
                nodes, hops, messages = get('/api/nodes'), get('/api/hops'), get('/api/messages')
                try:
                    settings = get('/api/settings')
                except (OSError, ValueError):
                    settings = {}
                with self.lock:
                    self.status, self.summary = status, summary
                    self.nodes = nodes if isinstance(nodes, list) else []
                    self.hops = hops if isinstance(hops, list) else []
                    self.messages = messages if isinstance(messages, list) else []
                    if self.ticker_kind == 'LISTEN' and self.messages and isinstance(self.messages[0], dict):
                        recent = self.messages[0]
                        self.ticker_kind = 'MESSAGE'
                        self.ticker_sender = str(recent.get('src') or 'Unknown sender')
                        self.ticker = str(recent.get('text') or 'New message').replace('\n', ' ')
                    self.settings = settings if isinstance(settings, dict) else {}
                    self.online = True
            except (OSError, ValueError, TypeError):
                with self.lock:
                    self.online = False
            time.sleep(4)

    def stream(self):
        while True:
            try:
                req = urllib.request.Request(BASE + '/api/stream', headers={'Accept': 'text/event-stream'})
                with urllib.request.urlopen(req, timeout=25) as response:
                    event, lines = '', []
                    for raw in response:
                        line = raw.decode('utf-8', 'replace').rstrip('\r\n')
                        if not line:
                            if event and lines:
                                try:
                                    self.on_event(event, json.loads('\n'.join(lines)))
                                except (ValueError, TypeError, AttributeError):
                                    pass
                            event, lines = '', []
                        elif line.startswith('event:'):
                            event = line[6:].strip()
                        elif line.startswith('data:'):
                            lines.append(line[5:].lstrip())
                time.sleep(2)
            except (OSError, ValueError, http.client.HTTPException):
                time.sleep(2)

    def on_event(self, event, value):
        if not isinstance(value, dict):
            return
        with self.lock:
            if event == 'status':
                self.status = value
            elif event == 'message':
                self.ticker = str(value.get('text') or 'New message').replace('\n', ' ')
                self.ticker_kind = 'MESSAGE'
                self.ticker_sender = str(value.get('src') or 'New message')
            elif event == 'packet':
                decoded = value.get('decoded') or {}
                if isinstance(decoded, str):
                    try:
                        decoded = json.loads(decoded)
                    except ValueError:
                        decoded = {}
                if not isinstance(decoded, dict):
                    decoded = {}
                path = decoded.get('path') or []
                ids = [decoded.get('src'), decoded.get('pubkey'), decoded.get('name')]
                if isinstance(path, list) and path:
                    ids.append(path[-1])
                self.events.append((time.monotonic(), [str(x).lower() for x in ids if x is not None]))
                self.events = self.events[-30:]
                self.ticker = str(decoded.get('type') or 'Packet received').replace('_', ' ').upper()
                self.ticker_kind = self.ticker
                self.ticker_sender = str(decoded.get('name') or decoded.get('src') or (path[-1] if isinstance(path, list) and path else 'Unknown origin'))
                self.ticker_proto = value.get('proto')


@functools.lru_cache(maxsize=8)
def qr_image(url, size):
    code = qrcode.QRCode(border=4, error_correction=qrcode.constants.ERROR_CORRECT_M)
    code.add_data(url)
    code.make(fit=True)
    matrix = code.get_matrix()
    scale = max(1, size // len(matrix))
    im = Image.new('RGB', (len(matrix)*scale, len(matrix)*scale), 'white')
    d = ImageDraw.Draw(im)
    for y, row in enumerate(matrix):
        for x, value in enumerate(row):
            if value:
                d.rectangle((x*scale, y*scale, (x+1)*scale-1, (y+1)*scale-1), fill=(6, 8, 12))
    return im


@functools.lru_cache(maxsize=512)
def clip(text, width, font):
    # The bundled fonts have no emoji glyphs; omit non-BMP emoji and selectors
    # rather than showing distracting missing-glyph boxes in station names.
    text = ''.join(ch for ch in str(text).replace('\n', ' ') if ord(ch) <= 0xffff and ch not in ('\ufe0f', '\u200d'))
    if font.getlength(text) <= width:
        return text
    while text and font.getlength(text + '…') > width:
        text = text[:-1]
    return text + '…'


PRESETS = {
    'meshcore_nl_sf7': 'MeshCore NL',
    'meshcore_eu_narrow_sf8': 'MeshCore EU/UK Narrow',
    'meshcore_us_sf7': 'MeshCore US',
    'meshtastic_longfast': 'Meshtastic LongFast',
    'meshtastic_mediumfast': 'Meshtastic MediumFast',
    'meshtastic_us_longfast': 'Meshtastic US LongFast',
    'raw_lorawan_868.1_sf7': 'LoRaWAN 868.1',
}


def preset_name(value):
    value = str(value or '')
    if value in PRESETS:
        return PRESETS[value]
    if value.startswith('meshcore_'):
        return 'MeshCore · custom'
    if value.startswith('meshtastic_'):
        return 'Meshtastic · custom'
    return 'Raw · custom' if value else 'Waiting for meshscan'


def home_override():
    raw = os.environ.get('RADAR_HOME')
    if not raw:
        return None
    try:
        lat, lon = map(float, raw.split(','))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return {'lat': lat, 'lon': lon}
    except ValueError:
        pass
    print('Ignoring invalid RADAR_HOME (expected latitude,longitude)', file=sys.stderr)
    return None


def age_seconds(node, now):
    ts = node.get('last_seen')
    return max(0, now-ts) if isinstance(ts, (int, float)) else float('inf')


def age_label(node, now):
    age = age_seconds(node, now)
    if not math.isfinite(age):
        return 'unknown'
    if age < 60:
        return '%ds ago' % age
    if age < 3600:
        return '%dm ago' % (age//60)
    if age < 86400:
        return '%dh ago' % (age//3600)
    return '%dd ago' % (age//86400)


def distance_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (a['lat'], a['lon'], b['lat'], b['lon']))
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 12742*math.asin(min(1, math.sqrt(max(0, h))))


def visible_nodes(nodes, show_all, now):
    return [n for n in nodes if isinstance(n, dict) and map_tiles.valid_location(n)
            and (show_all or age_seconds(n, now) < 86400)]


def marker(draw, node, x, y, bright, selected=False):
    color_value = color(node.get('proto'))
    factor = 1 if bright else .52
    c = tuple(int(v*factor) for v in color_value)
    kind = node.get('kind')
    x, y = int(x), int(y)
    if selected:
        draw.ellipse((x-7, y-7, x+7, y+7), outline=(235, 240, 241), width=1)
    if kind == 'repeater':
        draw.polygon(((x,y-4),(x+4,y),(x,y+4),(x-4,y)), fill=c)
    elif kind == 'room':
        draw.rectangle((x-3,y-3,x+3,y+3), outline=c, width=1)
    elif kind == 'sensor':
        draw.polygon(((x,y-4),(x+4,y+3),(x-4,y+3)), outline=c)
    else:
        draw.ellipse((x-3,y-3,x+3,y+3), fill=c)


def draw_nodes(base, nodes, view, home, selected_id, now):
    image = base.copy()
    d = ImageDraw.Draw(image)
    cx, cy, z = view
    positions = []
    for node in nodes:
        wx, wy = map_tiles.project(node['lat'], node['lon'], z)
        x = wx-cx+map_tiles.MAP_WIDTH/2
        y = wy-cy+map_tiles.MAP_HEIGHT/2
        if 0 <= x < map_tiles.MAP_WIDTH and 0 <= y < map_tiles.MAP_HEIGHT-12:
            positions.append((node, x, y))
    # Old points are drawn first, then the newest points and the selection.
    positions.sort(key=lambda p: (p[0].get('node_id') == selected_id, -age_seconds(p[0], now)))
    for node, x, y in positions:
        marker(d, node, x, y, age_seconds(node, now) < 3600, node.get('node_id') == selected_id)
    if home and map_tiles.valid_location(home):
        wx, wy = map_tiles.project(home['lat'], home['lon'], z)
        x, y = wx-cx+map_tiles.MAP_WIDTH/2, wy-cy+map_tiles.MAP_HEIGHT/2
        if 0 <= x < map_tiles.MAP_WIDTH and 0 <= y < map_tiles.MAP_HEIGHT:
            d.ellipse((x-6,y-6,x+6,y+6), outline=(250, 192, 112))
            d.polygon(((x,y-3),(x+3,y),(x,y+3),(x-3,y)), fill=(250, 192, 112))
    # Selected label first; then newest, preferring repeaters for equally recent nodes.
    labels = sorted(positions, key=lambda p: (p[0].get('node_id') != selected_id, age_seconds(p[0], now), p[0].get('kind') != 'repeater'))
    occupied = []
    for node, x, y in labels:
        name = node.get('short_name') or node.get('name')
        if not name:
            continue
        text = clip(str(name), 64, SMALL)
        width = int(SMALL.getlength(text))+4
        for lx, ly in ((x+5,y-11), (x-width-5,y+3), (x+5,y+3), (x-width-5,y-11)):
            box = (int(lx), int(ly), int(lx+width), int(ly+9))
            if box[0] < 0 or box[2] >= map_tiles.MAP_WIDTH or box[1] < 0 or box[3] >= 155:
                continue
            if any(not (box[2] < b[0] or box[0] > b[2] or box[3] < b[1] or box[1] > b[3]) for b in occupied):
                continue
            d.rounded_rectangle(box, radius=2, fill=(8,19,26))
            d.text((box[0]+2,box[1]-1), text, font=SMALL, fill=(185, 208, 216))
            occupied.append(box)
            break
        if len(occupied) >= 4:
            break
    d.rectangle((0, 158, 190, 169), fill=(7, 15, 19))
    d.text((2,158), '© OpenStreetMap  ·  OpenMapTiles  ·  OpenFreeMap', font=ATTR, fill=(145, 168, 173))
    return image, positions


def build_map_panel(data, nodes, selected, now, home, on_map):
    im = Image.new('RGB', (130, 170), (6, 8, 12))
    d = ImageDraw.Draw(im)
    with data.lock:
        status, summary, online = data.status, data.summary, data.online
        kind = getattr(data, 'ticker_kind', 'LISTEN')
        sender = getattr(data, 'ticker_sender', 'Waiting for traffic')
    mode = str(status.get('mode') or '').upper()
    accent = GREEN if mode == 'CHAT' else BLUE
    d.line((0, 6, 0, 165), fill=(35, 50, 57))
    d.rounded_rectangle((7, 5, 51, 20), 4, fill=(14,38,37) if mode == 'CHAT' else (14,29,44))
    d.text((11,6), mode if mode in ('CHAT','SCAN') else '----', font=MONO, fill=accent)
    d.ellipse((65,10,69,14), fill=GREEN if online else (255,184,107))
    d.text((73,6), 'LIVE' if online else 'OFFLINE', font=MONO, fill=GREEN if online else (255,184,107))
    d.text((7,23), clip(preset_name(status.get('config')), 118, BOLD), font=BOLD, fill=TEXT)
    freq = status.get('freq')
    desc = '%s MHz / SF%s' % (freq, status.get('sf','?')) if freq is not None else 'Waiting for API'
    d.text((7,37), clip(desc, 118, SMALL), font=SMALL, fill=MUTED)
    recent = sum(1 for n in nodes if isinstance(n, dict) and age_seconds(n, now)<3600)
    no_gps = sum(1 for n in nodes if isinstance(n, dict) and not map_tiles.valid_location(n))
    d.text((7,47), str(on_map), font=BIG, fill=TEXT)
    d.text((72,47), str(recent), font=BIG, fill=BLUE)
    d.text((8,73), 'ON MAP', font=SMALL, fill=MUTED)
    d.text((73,73), 'LAST 1H', font=SMALL, fill=MUTED)
    d.text((7,84), '%s WITHOUT GPS' % no_gps, font=SMALL, fill=(133,159,165))
    d.line((7,97,123,97), fill=(31,44,52))
    if selected:
        name = selected.get('short_name') or selected.get('name') or selected.get('node_id') or 'Unknown'
        d.text((7,99), clip(name, 116, BOLD), font=BOLD, fill=TEXT)
        desc = '%s · %s' % (str(selected.get('kind') or 'node').upper(), age_label(selected, now))
        d.text((7,113), clip(desc, 116, SMALL), font=SMALL, fill=BLUE)
        detail = '%s pkts' % selected.get('packets', 0)
        if home and map_tiles.valid_location(home):
            detail += ' · %.1f km' % distance_km(home, selected)
        d.text((7,123), clip(detail, 116, SMALL), font=SMALL, fill=MUTED)
    else:
        d.text((7,99), clip(str(kind).upper(), 116, SMALL), font=SMALL, fill=BLUE)
        d.text((7,110), clip(sender, 116, FONT), font=FONT, fill=TEXT)
        d.text((7,123), 'TAB SELECT  F FIT', font=SMALL, fill=MUTED)
    hist = status.get('noise_hist') or []
    vals = [r[1] for r in hist[-35:] if isinstance(r,list) and len(r)>1 and isinstance(r[1], (int,float))]
    noise = status.get('noise')
    d.text((7,137), 'NOISE', font=SMALL, fill=MUTED)
    d.text((95,137), ('%.0f' % noise) if isinstance(noise,(int,float)) else '--', font=SMALL, fill=TEXT)
    if len(vals)>1:
        lo, hi = min(vals)-1, max(vals)+1
        d.line([(7+i*116/(len(vals)-1), 158-(v-lo)/(hi-lo)*10) for i,v in enumerate(vals)], fill=(55,153,168))
    if selected:
        d.text((7,160), clip(str(kind).upper() + ' · ' + str(sender), 116, SMALL), font=SMALL, fill=BLUE)
    else:
        d.text((7,160), 'Q EXIT  SHIFT+Q QR', font=SMALL, fill=(108,145,151))
    return im


class Renderer:
    def __init__(self):
        self.override_home = home_override()
        self.home = self.override_home
        self.worker = map_tiles.TileWorker()
        self.view = None
        self.positions = []
        self.nodes = []
        self.selected_id = None
        self.show_all = False
        self.panel_at = 0
        self.cache_key = None
        self.map_image = None
        self.panel = None
        self.last_missing = 0
        self.autofit = True

    def fit_nodes(self):
        self.view = map_tiles.fit(visible_nodes(self.nodes, self.show_all, time.time()))
        if self.view is None and self.home and map_tiles.valid_location(self.home):
            self.view = (*map_tiles.project(self.home['lat'], self.home['lon'], 11), 11)
        self.autofit = self.view is None
        self.cache_key = None

    def change_view(self, code):
        if code == 33:  # F
            self.fit_nodes()
            return
        if code == 30:  # A: include old nodes
            self.show_all = not self.show_all
            self.cache_key = None
            self.panel_at = 0
            return
        if code == 35 and self.home and map_tiles.valid_location(self.home):
            z = self.view[2] if self.view else 11
            self.view = (*map_tiles.project(self.home['lat'], self.home['lon'], z), z)
            self.cache_key = None
            return
        if code in (15, 57):
            available = sorted(visible_nodes(self.nodes, self.show_all, time.time()), key=lambda n: age_seconds(n, time.time()))
            if available:
                ids = [n.get('node_id') for n in available]
                index = (ids.index(self.selected_id)+1) % len(ids) if self.selected_id in ids else 0
                node = available[index]
                self.selected_id = node.get('node_id')
                z = self.view[2] if self.view else 11
                self.view = (*map_tiles.project(node['lat'], node['lon'], z), z)
                self.cache_key = None
                self.panel_at = 0
            return
        if not self.view:
            return
        x,y,z = self.view
        if code in (105,106,103,108):
            dx, dy = {105:(-40,0),106:(40,0),103:(0,-40),108:(0,40)}[code]
            self.view = (x+dx,y+dy,z)
        elif code in (12, 13, 71, 72, 73):
            # Cardputer Sym layer sends 71='-', 72='+', 73='='; standard
            # keyboards instead send KEY_MINUS=12 / KEY_EQUAL=13.
            newz = max(2, min(14, z+(-1 if code in (12, 71) else 1)))
            factor = 2**(newz-z)
            self.view = (x*factor,y*factor,newz)
        else:
            return
        self.cache_key = None

    def render(self, data, now, qr_view, selected, url):
        if qr_view:
            im = Image.new('RGB', (W,H), (6,8,12))
            d = ImageDraw.Draw(im)
            qr = qr_image(url,150)
            im.paste(qr,(9,(H-qr.height)//2))
            d.text((174,23),'DASHBOARD',font=BOLD,fill=BLUE)
            d.text((174,47),'Scan to explore',font=FONT,fill=TEXT)
            d.text((174,62),'your live mesh.',font=FONT,fill=MUTED)
            d.text((174,91),url,font=SMALL,fill=TEXT)
            d.line((174,114,311,114),fill=(33,54,65))
            d.text((174,126),'SHIFT+Q  BACK',font=MONO,fill=MUTED)
            return im
        wall = time.time()
        with data.lock:
            nodes, settings, events = data.nodes, data.settings, list(data.events)
        self.nodes = nodes
        self.home = self.override_home or settings.get('home')
        if self.autofit and (nodes or data.online):
            self.fit_nodes()
        selected_node = next((n for n in nodes if isinstance(n, dict) and n.get('node_id') == self.selected_id and map_tiles.valid_location(n)), None)
        if self.view:
            view = (round(self.view[0]), round(self.view[1]), self.view[2])
            self.worker.request(view)
            with self.worker.lock:
                result = self.worker.result
            tile_ready = result and result[0] == view
        else:
            view, result, tile_ready = None, None, False
        if tile_ready:
            visible = visible_nodes(nodes, self.show_all, wall)
            key = (view, id(nodes), self.selected_id, self.show_all, str(self.home), id(result[1]))
            if key != self.cache_key:
                self.map_image, self.positions = draw_nodes(result[1], visible, view, self.home, self.selected_id, wall)
                self.last_missing = result[2]
                self.cache_key = key
                self.panel_at = 0
            image = self.map_image.copy()
            d = ImageDraw.Draw(image)
            if self.last_missing:
                d.rectangle((4,4,133,17), fill=(20,25,28))
                d.text((7,5),'MAP TILES UNAVAILABLE',font=SMALL,fill=(255,184,107))
            # Gentle pulse only for recently heard locations.
            pulse = int(75+35*(1+math.sin(now*2))/2)
            for node,x,y in self.positions:
                if age_seconds(node, wall) < 3600:
                    d.ellipse((x-6,y-6,x+6,y+6), outline=(pulse//2,pulse,pulse))
            for t, ids in events:
                age = now-t
                if not 0 <= age < 1.4:
                    continue
                match = next(((x,y) for n,x,y in self.positions if any(s and (str(n.get('node_id') or '').lower().startswith(s) or s.startswith(str(n.get('node_id') or 'not-an-id').lower()) or str(n.get('name') or '').lower()==s) for s in ids)), None)
                if match:
                    x,y = match
                    r,v = 3+age*11, int(200*(1-age/1.4))
                    d.ellipse((x-r,y-r,x+r,y+r), outline=(v//2,v,v))
        else:
            image = Image.new('RGB', (map_tiles.MAP_WIDTH,H), (10,17,22))
            d = ImageDraw.Draw(image)
            d.text((14,70), 'LOADING MAP…' if view else 'WAITING FOR LOCATIONS…', font=BOLD, fill=(103,153,163))
            d.text((3,158),'© OpenStreetMap  ·  OpenMapTiles  ·  OpenFreeMap',font=ATTR,fill=(145,168,173))
        if now >= self.panel_at:
            self.panel = build_map_panel(data, nodes, selected_node, wall, self.home, len(self.positions) if tile_ready else 0)
            self.panel_at = now+1
        frame = Image.new('RGB',(W,H))
        frame.paste(image,(0,0))
        frame.paste(self.panel,(190,0))
        return frame


def render(data, now, qr_view, selected, url):
    """Compatibility entry point for headless rendering tests."""
    return Renderer().render(data, now, qr_view, selected, url)


def main():
    data, url, renderer = Data(), dashboard_url(), Renderer()
    fb = os.open(os.environ.get('APPLAUNCH_LINUX_FBDEV_DEVICE','/dev/fb0'),os.O_RDWR)
    mapping = mmap.mmap(fb,W*H*2,mmap.MAP_SHARED,mmap.PROT_READ|mmap.PROT_WRITE)
    keyboard = os.environ.get('APPLAUNCH_LINUX_KEYBOARD_DEVICE','/dev/input/by-path/platform-3f804000.i2c-event')
    keyfd = os.open(keyboard,os.O_RDONLY|os.O_NONBLOCK)
    event = struct.Struct('@llHHi')
    qr_view, running, shifted = False, True, False
    frames, started = 0, time.monotonic()
    try:
        while running:
            start = time.monotonic()
            while select.select([keyfd],[],[],0)[0]:
                raw = os.read(keyfd,event.size)
                if len(raw)!=event.size:
                    break
                _,_,kind,code,value = event.unpack(raw)
                if kind!=1:
                    continue
                if code in (42,54):
                    shifted = bool(value)
                elif value == 1:
                    if code == 1 or (code == 16 and not shifted):
                        running = False
                    elif code == 16 and shifted:
                        qr_view = not qr_view
                    else:
                        renderer.change_view(code)
            frame = renderer.render(data,start,qr_view,0,url)
            rgb = np.asarray(frame,dtype=np.uint16)
            pixels = ((rgb[:,:,0]>>3)<<11)|((rgb[:,:,1]>>2)<<5)|(rgb[:,:,2]>>3)
            mapping[:] = pixels.astype('<u2').tobytes()
            frames += 1
            with data.lock:
                has_pulses = any(start-t < 1.4 for t, _ in data.events)
            has_recent = any(age_seconds(n, time.time()) < 3600 for n, _, _ in renderer.positions)
            fps = 12 if not qr_view and (has_recent or has_pulses) else 4
            delay = 1/fps-(time.monotonic()-start)
            if delay>0:
                time.sleep(delay)
    finally:
        elapsed = time.monotonic()-started
        print('map: %.2f fps (%d frames in %.1fs)'%(frames/elapsed,frames,elapsed),file=sys.stderr,flush=True)
        os.close(keyfd)
        mapping.close()
        os.close(fb)


if __name__ == '__main__':
    main()
