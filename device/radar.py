#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Meshscan's read-only Cardputer framebuffer radar."""
import functools
import hashlib
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

import numpy as np
import qrcode
from PIL import Image, ImageDraw, ImageFont

BASE = 'http://127.0.0.1:8080'
W, H = 320, 170
BLUE, GREEN, PURPLE = (94, 177, 255), (79, 220, 152), (185, 150, 255)
TEXT, MUTED = (232, 236, 242), (139, 148, 163)
FONTS = '/usr/share/fonts/truetype/dejavu/'
FONT = ImageFont.truetype(FONTS + 'DejaVuSans.ttf', 9)
BOLD = ImageFont.truetype(FONTS + 'DejaVuSans-Bold.ttf', 10)
BIG = ImageFont.truetype(FONTS + 'DejaVuSans-Bold.ttf', 19)
MONO = ImageFont.truetype(FONTS + 'DejaVuSansMono.ttf', 8)


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


def position(node, home):
    ident = str(node.get('node_id') or node.get('id') or '?')
    lat, lon = node.get('lat'), node.get('lon')
    if isinstance(home, dict) and all(isinstance(x, (int, float)) for x in (lat, lon, home.get('lat'), home.get('lon'))):
        a, b, c, d = map(math.radians, (home['lat'], home['lon'], lat, lon))
        dl = d - b
        angle = math.atan2(math.sin(dl) * math.cos(c), math.cos(a) * math.sin(c) - math.sin(a) * math.cos(c) * math.cos(dl))
        h = math.sin((c-a)/2)**2 + math.cos(a)*math.cos(c)*math.sin(dl/2)**2
        distance = 12742 * math.asin(min(1, math.sqrt(max(0, h))))
        radius = min(70, 7 + 63 * math.log1p(distance) / math.log(101))
    else:
        angle = int.from_bytes(hashlib.sha256(ident.encode()).digest()[:4], 'big') / 2**32 * math.tau
        rssi = node.get('last_rssi')
        rssi = rssi if isinstance(rssi, (int, float)) else -115
        radius = max(8, min(70, 12 + (-rssi - 45) * .8))
    return 82 + math.sin(angle)*radius, 85 - math.cos(angle)*radius, angle


class Data:
    def __init__(self):
        self.lock = threading.Lock()
        self.status, self.summary, self.nodes, self.hops, self.messages, self.settings = {}, {}, [], [], [], {}
        self.online = False
        self.events = []
        self.ticker = 'Listening for packets'
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


@functools.lru_cache(maxsize=256)
def clip(text, width, font):
    text = str(text).replace('\n', ' ')
    while text and font.getlength(text) > width:
        text = text[:-1]
    return text


def render(data, now, qr_view, selected, url):
    with data.lock:
        status, summary, nodes, hops, settings = data.status, data.summary, data.nodes, data.hops, data.settings
        online, ticker, events, messages = data.online, data.ticker, list(data.events), data.messages
    im = Image.new('RGB', (W, H), (6, 8, 12))
    d = ImageDraw.Draw(im)
    if qr_view:
        qr = qr_image(url, 142)
        im.paste(qr, (12, (H-qr.height)//2))
        d.text((169, 25), 'OPEN DASHBOARD', font=BOLD, fill=BLUE)
        d.text((169, 50), 'Scan to explore', font=FONT, fill=TEXT)
        d.text((169, 65), 'the live mesh.', font=FONT, fill=MUTED)
        for i, part in enumerate((url[:19], url[19:])):
            d.text((169, 92+i*12), part, font=MONO, fill=TEXT)
        d.text((169, 143), 'SHIFT+Q  BACK', font=MONO, fill=MUTED)
        return im
    cx, cy, rad = 82, 85, 76
    d.ellipse((cx-rad, cy-rad, cx+rad, cy+rad), fill=(8, 16, 23), outline=(32, 65, 79), width=1)
    for r in (19, 38, 57):
        d.ellipse((cx-r, cy-r, cx+r, cy+r), outline=(21, 43, 52))
    d.line((cx-rad, cy, cx+rad, cy), fill=(20, 41, 51))
    d.line((cx, cy-rad, cx, cy+rad), fill=(20, 41, 51))
    sweep = (now * .8) % math.tau
    for i in range(40, 0, -1):
        a = sweep - i * .015
        intensity = int(7 + 24*(1-i/40))
        d.line((cx, cy, cx+rad*math.sin(a), cy-rad*math.cos(a)), fill=(intensity//3, intensity, int(intensity*1.2)))
    d.line((cx, cy, cx+rad*math.sin(sweep), cy-rad*math.cos(sweep)), fill=(61, 152, 177), width=1)
    home = settings.get('home')
    all_nodes = [x for x in nodes if isinstance(x, dict)] + [dict(x, proto='meshcore', kind='relay') for x in hops if isinstance(x, dict)]
    all_nodes.sort(key=lambda x: x.get('last_rssi') if isinstance(x.get('last_rssi'), (float, int)) else -999, reverse=True)
    positions = []
    for node in all_nodes:
        x, y, a = position(node, home)
        positions.append((node, x, y))
        age = (sweep - a) % math.tau
        glow = max(0, 1 - age / 1.5)
        c = color(node.get('proto'))
        factor = .38 + .62*glow
        c = tuple(int(v*factor) for v in c)
        snr = node.get('last_snr')
        radius = 2 if not isinstance(snr, (int, float)) or snr < 4 else 3
        if node.get('kind') in ('repeater', 'relay'):
            d.polygon([(x, y-radius-1), (x+radius+1,y), (x,y+radius+1), (x-radius-1,y)], fill=c)
        else:
            d.ellipse((x-radius,y-radius,x+radius,y+radius), fill=c)
    for t, ids in events:
        age = now-t
        if not 0 <= age < 1.4:
            continue
        matches = [(x,y) for node,x,y in positions if any(s and (str(node.get('node_id') or node.get('id') or '').lower().startswith(s) or s.startswith(str(node.get('node_id') or node.get('id') or 'not-an-id').lower()) or str(node.get('name') or '').lower() == s) for s in ids)]
        x,y = matches[0] if matches else (cx,cy)
        r = 2 + age*13
        v = int(180*(1-age/1.4))
        d.ellipse((x-r,y-r,x+r,y+r), outline=(v//2,v,v))
    d.ellipse((80,83,84,87), fill=(150,220,228))
    d.text((7, 1), 'N', font=MONO, fill=(69, 111, 123))
    d.line((165, 10, 165, 160), fill=(30, 38, 48))
    mode = str(status.get('mode') or 'offline').upper()
    accent = BLUE if online else (255,184,107)
    d.rounded_rectangle((174, 8, 218, 23), radius=4, fill=(17, 35, 49) if online else (45,30,20))
    d.text((179, 10), clip(mode, 37, MONO), font=MONO, fill=accent)
    d.text((226, 10), 'LIVE' if online else 'OFFLINE', font=MONO, fill=accent)
    preset = str(status.get('config') or 'waiting for meshscan...').replace('meshcore_', '').upper()
    d.text((174, 30), clip(preset, 140, BOLD), font=BOLD, fill=TEXT if online else MUTED)
    freq = status.get('freq')
    desc = ('%s MHz  /  SF%s' % (freq, status.get('sf', '?'))) if freq is not None else 'reconnecting to API'
    d.text((174, 44), clip(desc, 140, MONO), font=MONO, fill=MUTED)
    d.text((174, 62), str(summary.get('n', '--')), font=BIG, fill=TEXT)
    d.text((257, 62), str(summary.get('nodes', '--')), font=BIG, fill=BLUE)
    d.text((174, 84), 'PACKETS', font=MONO, fill=MUTED)
    d.text((257, 84), 'NODES', font=MONO, fill=MUTED)
    d.line((174, 99, 312, 99), fill=(30, 38, 48))
    if selected and positions:
        node = positions[(selected-1) % len(positions)][0]
        ticker = str(node.get('name') or node.get('node_id') or node.get('id'))
    elif ticker == 'Listening for packets' and messages:
        ticker = messages[0].get('text') or ticker
    d.text((174, 104), clip(ticker, 138, FONT), font=FONT, fill=TEXT)
    hist = status.get('noise_hist') or []
    vals = [row[1] for row in hist[-40:] if isinstance(row, list) and len(row)>1 and isinstance(row[1], (float,int))]
    d.text((174, 121), 'NOISE', font=MONO, fill=MUTED)
    noise = status.get('noise')
    d.text((174, 133), ('%.0f dBm' % noise) if isinstance(noise, (int,float)) else '-- dBm', font=MONO, fill=TEXT)
    if len(vals) > 1:
        lo, hi = min(vals)-1, max(vals)+1
        points = [(226+i*47/(len(vals)-1), 145-(v-lo)/(hi-lo)*20) for i,v in enumerate(vals)]
        d.line(points, fill=(61, 143, 158), width=1)
    qr = qr_image(url, 42)
    im.paste(qr, (275, 115))
    d.text((174, 157), 'SHIFT+Q QR  SPACE NODE', font=MONO, fill=(110,125,139))
    return im


def main():
    data, url = Data(), dashboard_url()
    fb = os.open(os.environ.get('APPLAUNCH_LINUX_FBDEV_DEVICE', '/dev/fb0'), os.O_RDWR)
    mapping = mmap.mmap(fb, W*H*2, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
    keyboard = os.environ.get('APPLAUNCH_LINUX_KEYBOARD_DEVICE', '/dev/input/by-path/platform-3f804000.i2c-event')
    keyfd = os.open(keyboard, os.O_RDONLY | os.O_NONBLOCK)
    event = struct.Struct('@llHHi')
    qr_view, selected, running, shifted = False, 0, True, False
    frames, started = 0, time.monotonic()
    try:
        while running:
            start = time.monotonic()
            while select.select([keyfd], [], [], 0)[0]:
                raw = os.read(keyfd, event.size)
                if len(raw) != event.size:
                    break
                _, _, kind, code, value = event.unpack(raw)
                if kind != 1:
                    continue
                if code in (42, 54):
                    shifted = bool(value)
                elif value == 1:
                    if code == 1 or (code == 16 and not shifted):
                        running = False
                    elif code == 16 and shifted:
                        qr_view = not qr_view
                    elif code in (15, 57):
                        selected += 1
            frame = render(data, start, qr_view, selected, url)
            rgb = np.asarray(frame, dtype=np.uint16)
            pixels = ((rgb[:,:,0] >> 3) << 11) | ((rgb[:,:,1] >> 2) << 5) | (rgb[:,:,2] >> 3)
            mapping[:] = pixels.astype('<u2').tobytes()
            frames += 1
            delay = 1/30 - (time.monotonic()-start)
            if delay > 0:
                time.sleep(delay)
    finally:
        elapsed = time.monotonic()-started
        print('radar: %.2f fps (%d frames in %.1fs)' % (frames/elapsed, frames, elapsed), file=sys.stderr, flush=True)
        os.close(keyfd)
        mapping.close()
        os.close(fb)


if __name__ == '__main__':
    main()
