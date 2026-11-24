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
from PIL import Image, ImageDraw, ImageFont, ImageFilter

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
for font in (BOLD, BIG):
    font.set_variation_by_name('Bold')
CX, CY, RADIUS = 85, 85, 78


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
    return CX + math.sin(angle)*radius, CY - math.cos(angle)*radius, angle


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


@functools.lru_cache(maxsize=32)
def glow_sprite(rgb, size):
    """Cached soft radial glow; alpha is pre-rendered rather than blurred per frame."""
    extent = size * 4 + 5
    y, x = np.ogrid[-extent:extent+1, -extent:extent+1]
    alpha = (np.exp(-(x*x+y*y) / (2*(size*1.4)**2))*90).astype(np.uint8)
    image = Image.new('RGBA', (2*extent+1, 2*extent+1), rgb + (0,))
    image.putalpha(Image.fromarray(alpha, 'L'))
    return image


def glow(im, x, y, rgb, size):
    sprite = glow_sprite(tuple(rgb), size)
    im.paste(sprite, (int(x)-sprite.width//2, int(y)-sprite.height//2), sprite)


def plot_nodes(nodes, hops, home):
    combined = [n for n in nodes if isinstance(n, dict)]
    combined += [dict(n, proto='meshcore', kind='relay') for n in hops if isinstance(n, dict)]
    combined.sort(key=lambda n: n.get('last_rssi') if isinstance(n.get('last_rssi'), (int, float)) else -999, reverse=True)
    return [(n, *position(n, home)) for n in combined]


def build_scope(positions, home):
    im = Image.new('RGB', (W, H), (6, 8, 12))
    d = ImageDraw.Draw(im)
    d.ellipse((CX-RADIUS, CY-RADIUS, CX+RADIUS, CY+RADIUS), fill=(7, 17, 22), outline=(43, 77, 85))
    # Subtle static CRT grid and alternating scanlines, clipped inside the circle.
    for y in range(CY-RADIUS+3, CY+RADIUS-2, 3):
        span = int(math.sqrt(max(0, (RADIUS-2)**2 - (y-CY)**2)))
        d.line((CX-span, y, CX+span, y), fill=(8, 19, 24))
    for r in (19, 38, 57):
        d.ellipse((CX-r, CY-r, CX+r, CY+r), outline=(26, 52, 58))
    d.line((CX-RADIUS+1, CY, CX+RADIUS-1, CY), fill=(25, 47, 54))
    d.line((CX, CY-RADIUS+1, CX, CY+RADIUS-1), fill=(25, 47, 54))
    d.text((81, 10), 'N', font=SMALL, fill=(110, 152, 159))
    # Radius formula in position() maps these rings to approximately these values.
    for y, label in zip((103, 122, 141), ('-55', '-78', '-101') if not home else ('1.4', '9', '38')):
        d.text((89, y), label, font=SMALL, fill=(62, 101, 105))
    d.text((12, 151), 'dBm' if not home else 'km', font=SMALL, fill=(76, 121, 125))
    glow(im, CX, CY, (48, 162, 190), 4)
    d = ImageDraw.Draw(im)
    d.ellipse((CX-1, CY-1, CX+1, CY+1), fill=(135, 226, 233))
    for node, x, y, _ in reversed(positions):
        c = color(node.get('proto'))
        snr = node.get('last_snr')
        size = 2 if not isinstance(snr, (int, float)) or snr < 2 else 3
        glow(im, x, y, c, size)
        d = ImageDraw.Draw(im)
        dim = tuple(int(v*.68) for v in c)
        if node.get('kind') in ('repeater', 'relay'):
            d.polygon([(x,y-size), (x+size,y), (x,y+size), (x-size,y)], fill=dim)
        else:
            d.ellipse((x-size,y-size,x+size,y+size), outline=dim, width=1)
    # A few strong named stations, avoiding labels colliding with one another or the rim.
    occupied = []
    d = ImageDraw.Draw(im)
    for node, x, y, _ in positions:
        name = node.get('short_name') or node.get('name')
        if not name:
            continue
        label = clip(str(name), 42, SMALL)
        width = int(SMALL.getlength(label))+4
        options = [(x+5, y-10), (x-width-5, y+3), (x+5, y+3), (x-width-5, y-10)]
        for lx, ly in options:
            box = (int(lx), int(ly), int(lx+width), int(ly+9))
            if box[0] < 11 or box[2] > 159 or box[1] < 14 or box[3] > 155:
                continue
            if any(not (box[2] < b[0] or box[0] > b[2] or box[3] < b[1] or box[1] > b[3]) for b in occupied):
                continue
            d.rounded_rectangle(box, 2, fill=(8, 22, 29))
            d.text((box[0]+2, box[1]-1), label, font=SMALL, fill=(160, 190, 199))
            occupied.append(box)
            break
        if len(occupied) == 3:
            break
    return im


def build_panel(data, positions, selected):
    im = Image.new('RGB', (150, H), (6, 8, 12))
    d = ImageDraw.Draw(im)
    with data.lock:
        status, summary = data.status, data.summary
        online = data.online
        kind = getattr(data, 'ticker_kind', 'LISTEN')
        sender = getattr(data, 'ticker_sender', 'Waiting for traffic')
        proto = getattr(data, 'ticker_proto', None)
    mode = str(status.get('mode') or '').upper()
    accent = GREEN if mode == 'CHAT' else BLUE
    d.line((0, 10, 0, 159), fill=(32, 44, 52))
    d.rounded_rectangle((7, 7, 53, 23), radius=4, fill=(14, 38, 37) if mode == 'CHAT' else (14, 29, 44))
    d.text((13, 9), mode if mode in ('CHAT', 'SCAN') else '----', font=MONO, fill=accent)
    d.ellipse((76, 12, 80, 16), fill=GREEN if online else (255, 184, 107))
    d.text((85, 9), 'LIVE' if online else 'OFFLINE', font=MONO, fill=GREEN if online else (255, 184, 107))
    title = preset_name(status.get('config'))
    d.text((7, 28), clip(title, 137, BOLD), font=BOLD, fill=TEXT if online else MUTED)
    freq = status.get('freq')
    desc = '%s MHz  /  SF%s' % (freq, status.get('sf', '?')) if freq is not None else 'Reconnecting to API'
    d.text((7, 43), clip(desc, 137, MONO), font=MONO, fill=MUTED)
    d.text((7, 56), str(summary.get('n', '--')), font=BIG, fill=TEXT)
    d.text((89, 56), str(summary.get('nodes', '--')), font=BIG, fill=BLUE)
    d.text((8, 83), 'PACKETS', font=SMALL, fill=MUTED)
    d.text((90, 83), 'NODES', font=SMALL, fill=MUTED)
    d.line((7, 96, 143, 96), fill=(31, 44, 52))
    if selected and positions:
        node = positions[(selected-1) % len(positions)][0]
        kind, sender = 'NODE', str(node.get('name') or node.get('node_id') or node.get('id') or 'Unknown')
        proto = node.get('proto')
    chip = clip(kind.upper(), 54, SMALL)
    chip_width = min(59, max(29, int(SMALL.getlength(chip))+8))
    c = color(proto) if proto else BLUE
    d.rounded_rectangle((7, 102, 7+chip_width, 115), radius=3, fill=tuple(int(v*.13) for v in c))
    d.text((11, 103), chip, font=SMALL, fill=c)
    d.text((13+chip_width, 103), clip(sender, 130-chip_width, FONT), font=FONT, fill=TEXT)
    d.text((7, 122), 'NOISE FLOOR', font=SMALL, fill=MUTED)
    noise = status.get('noise')
    value = ('%.0f dBm' % noise) if isinstance(noise, (int, float)) else '-- dBm'
    d.text((103, 122), value, font=SMALL, fill=TEXT)
    hist = status.get('noise_hist') or []
    vals = [row[1] for row in hist[-48:] if isinstance(row, list) and len(row)>1 and isinstance(row[1], (int,float))]
    if len(vals) > 1:
        lo, hi = min(vals)-1, max(vals)+1
        points = [(8+i*134/(len(vals)-1), 148-(v-lo)/(hi-lo)*13) for i,v in enumerate(vals)]
        d.line(points, fill=(55, 153, 168), width=1)
    else:
        d.line((8, 145, 142, 145), fill=(31, 55, 61))
    d.text((7, 155), 'SHIFT+Q  DASHBOARD', font=SMALL, fill=(108, 145, 151))
    return im


class Renderer:
    def __init__(self):
        self.home = home_override()
        self.scope_key = None
        self.panel_at = 0
        self.positions = []
        self.scope = None
        self.panel = None
        self.trail = np.zeros((80, 80), dtype=np.float32)
        self.beams = []
        for i in range(180):
            angle = i * math.tau / 180
            mask = Image.new('L', (80, 80))
            d = ImageDraw.Draw(mask)
            d.line((40, 40, 40+37*math.sin(angle), 40-37*math.cos(angle)), fill=200, width=1)
            self.beams.append(np.asarray(mask, dtype=np.float32))

    def render(self, data, now, qr_view, selected, url):
        if qr_view:
            im = Image.new('RGB', (W, H), (6, 8, 12))
            d = ImageDraw.Draw(im)
            qr = qr_image(url, 150)
            im.paste(qr, (9, (H-qr.height)//2))
            d.text((174, 23), 'DASHBOARD', font=BOLD, fill=BLUE)
            d.text((174, 47), 'Scan to explore', font=FONT, fill=TEXT)
            d.text((174, 62), 'your live mesh.', font=FONT, fill=MUTED)
            d.text((174, 91), url, font=SMALL, fill=TEXT)
            d.line((174, 114, 311, 114), fill=(33, 54, 65))
            d.text((174, 126), 'SHIFT+Q  BACK', font=MONO, fill=MUTED)
            return im
        with data.lock:
            nodes, hops, settings = data.nodes, data.hops, data.settings
            events = list(data.events)
        home = self.home or settings.get('home')
        key = (id(nodes), id(hops), str(home))
        if key != self.scope_key:
            self.positions = plot_nodes(nodes, hops, home)
            self.scope = build_scope(self.positions, home)
            self.scope_key = key
            self.panel_at = 0
        if now >= self.panel_at:
            self.panel = build_panel(data, self.positions, selected)
            self.panel_at = now + 0.8
        im = self.scope.copy()
        im.paste(self.panel, (170, 0))
        sweep = (now * .8) % math.tau
        self.trail *= .90
        np.add(self.trail, self.beams[int(sweep / math.tau * 180) % 180], out=self.trail)
        np.minimum(self.trail, 200, out=self.trail)
        # The decaying half-resolution intensity field supplies a soft phosphor trail.
        alpha = Image.fromarray(self.trail.astype(np.uint8), 'L').resize((156, 156), Image.Resampling.BILINEAR)
        im.paste((37, 142, 157), (7, 7, 163, 163), alpha)
        d = ImageDraw.Draw(im)
        d.line((CX, CY, CX+75*math.sin(sweep), CY-75*math.cos(sweep)), fill=(87, 191, 208))
        for node, x, y, angle in self.positions:
            age = (sweep - angle) % math.tau
            if age < .16:
                glow(im, x, y, color(node.get('proto')), 3)
                d = ImageDraw.Draw(im)
                d.ellipse((x-1,y-1,x+1,y+1), fill=TEXT)
        for t, ids in events:
            age = now-t
            if not 0 <= age < 1.4:
                continue
            match = next(((x,y) for n,x,y,_ in self.positions if any(s and (str(n.get('node_id') or n.get('id') or '').lower().startswith(s) or s.startswith(str(n.get('node_id') or n.get('id') or 'not-an-id').lower()) or str(n.get('name') or '').lower() == s) for s in ids)), (CX,CY))
            x, y = match
            r, v = 2+age*13, int(190*(1-age/1.4))
            d.ellipse((x-r,y-r,x+r,y+r), outline=(v//2,v,v))
        return im


def render(data, now, qr_view, selected, url):
    """Compatibility entry point for headless rendering tests."""
    return Renderer().render(data, now, qr_view, selected, url)


def main():
    data, url, renderer = Data(), dashboard_url(), Renderer()
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
                        renderer.panel_at = 0
            frame = renderer.render(data, start, qr_view, selected, url)
            rgb = np.asarray(frame, dtype=np.uint16)
            pixels = ((rgb[:,:,0] >> 3) << 11) | ((rgb[:,:,1] >> 2) << 5) | (rgb[:,:,2] >> 3)
            mapping[:] = pixels.astype('<u2').tobytes()
            frames += 1
            delay = 1/24 - (time.monotonic()-start)
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
