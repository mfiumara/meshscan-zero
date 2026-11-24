# SPDX-License-Identifier: GPL-3.0-or-later
"""OpenFreeMap vector tile fetching and a small, asynchronous Pillow renderer."""
import gzip
import json
import math
import os
from pathlib import Path
import threading
import urllib.request

import mapbox_vector_tile
from PIL import Image, ImageDraw, ImageFont

TILE = 256
MAP_WIDTH, MAP_HEIGHT = 190, 170
USER_AGENT = 'meshscan-zero-map/1.0 (+https://github.com/mfiumara/meshscan-zero)'
DEFAULT_TILEJSON = 'https://tiles.openfreemap.org/planet'
FONT = ImageFont.truetype(str(Path(__file__).parent / 'fonts' / 'Inter.ttf'), 9)


def project(lat, lon, z):
    lat = max(-85.05112878, min(85.05112878, float(lat)))
    size = TILE * (2 ** z)
    y = (1-math.asinh(math.tan(math.radians(lat)))/math.pi)/2
    return ((float(lon)+180)/360*size, y*size)


def unproject(x, y, z):
    size = TILE * (2 ** z)
    return math.degrees(math.atan(math.sinh(math.pi*(1-2*y/size)))), x/size*360-180


def valid_location(node):
    lat, lon = node.get('lat'), node.get('lon')
    return (isinstance(lat, (float, int)) and isinstance(lon, (float, int))
            and math.isfinite(lat) and math.isfinite(lon) and -85.051 <= lat <= 85.051 and -180 <= lon <= 180)


def fit(nodes):
    points = [project(n['lat'], n['lon'], 0) for n in nodes if valid_location(n)]
    if not points:
        return None
    xs, ys = zip(*points)
    midx, midy = (min(xs)+max(xs))/2, (min(ys)+max(ys))/2
    for z in range(14, 1, -1):
        if (max(xs)-min(xs))*2**z <= MAP_WIDTH-24 and (max(ys)-min(ys))*2**z <= MAP_HEIGHT-24:
            return midx*2**z, midy*2**z, z
    return midx*4, midy*4, 2


def _shapes(geometry):
    kind, coords = geometry.get('type'), geometry.get('coordinates', [])
    if kind == 'Polygon':
        return [coords]
    if kind == 'MultiPolygon':
        return coords
    return []


def draw_tile(raw):
    """Draw water, selected roads, borders and place names from OpenMapTiles layers."""
    if raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    layers = mapbox_vector_tile.decode(raw, default_options={'y_coord_down': True})
    im = Image.new('RGB', (TILE, TILE), (12, 19, 24))
    d = ImageDraw.Draw(im)
    def transform(layer, coords):
        scale = TILE/layer.get('extent', 4096)
        return [(int(x*scale), int(y*scale)) for x, y in coords]
    for name, fill in [('landcover', (15, 27, 29)), ('park', (15, 28, 27)), ('water', (12, 35, 51))]:
        layer = layers.get(name, {})
        for f in layer.get('features', []):
            for poly in _shapes(f.get('geometry', {})):
                if poly and len(poly[0]) >= 3:
                    d.polygon(transform(layer, poly[0]), fill=fill)
                    for hole in poly[1:]:
                        if len(hole) >= 3:
                            d.polygon(transform(layer, hole), fill=(12, 19, 24))
    for name in ('waterway', 'boundary', 'transportation'):
        layer = layers.get(name, {})
        for f in layer.get('features', []):
            props, geom = f.get('properties', {}), f.get('geometry', {})
            kind = props.get('class', '')
            if name == 'transportation' and kind in ('path', 'track', 'service', 'rail', 'transit'):
                continue
            major = kind in ('motorway', 'trunk', 'primary', 'secondary')
            ink = (48, 73, 82) if name == 'waterway' else ((44, 56, 63) if name == 'boundary' else ((89, 105, 109) if major else (48, 61, 66)))
            segments = geom.get('coordinates', [])
            if geom.get('type') == 'LineString':
                segments = [segments]
            elif geom.get('type') != 'MultiLineString':
                continue
            for segment in segments:
                if len(segment) > 1:
                    d.line(transform(layer, segment), fill=ink, width=1)
    layer = layers.get('place', {})
    occupied = []
    features = sorted(layer.get('features', []), key=lambda f: f.get('properties', {}).get('rank', 999))
    for feature in features:
        prop, geom = feature.get('properties', {}), feature.get('geometry', {})
        name = prop.get('name:latin') or prop.get('name')
        if not name or geom.get('type') != 'Point' or prop.get('class') not in ('city', 'town', 'village'):
            continue
        x, y = transform(layer, [geom['coordinates']])[0]
        width = d.textlength(name, font=FONT)
        box = (x, y, x+width+3, y+11)
        if not (0 <= x < TILE and 0 <= y < TILE) or any(not (box[2] < b[0] or box[0] > b[2] or box[3] < b[1] or box[1] > b[3]) for b in occupied):
            continue
        d.text((x, y), name, font=FONT, fill=(136, 157, 163), stroke_width=1, stroke_fill=(10, 19, 25))
        occupied.append(box)
    return im


class TileWorker:
    def __init__(self, cache=None):
        self.cache = Path(cache or os.environ.get('MESH_MAP_CACHE', str(Path.home()/'.cache/meshscan-map')))
        self.cache.mkdir(parents=True, exist_ok=True)
        self.offline = os.environ.get('MESH_MAP_OFFLINE') == '1'
        self.tilejson_url = os.environ.get('MESH_MAP_TILEJSON', DEFAULT_TILEJSON)
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.requested = None
        self.result = None
        self.template = None
        threading.Thread(target=self.run, daemon=True, name='map-tiles').start()

    def request(self, view):
        with self.lock:
            if self.requested == view:
                return
            self.requested = view
        self.wake.set()

    def fetch(self, url):
        if self.offline:
            raise OSError('tile network disabled')
        req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT, 'Accept': '*/*'})
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.read()

    def get_template(self):
        if self.template:
            return self.template
        path = self.cache/'tilejson.json'
        try:
            metadata = json.loads(path.read_text())
        except (OSError, ValueError):
            metadata = json.loads(self.fetch(self.tilejson_url))
            path.write_text(json.dumps(metadata))
        self.template = metadata['tiles'][0]
        return self.template

    def tile(self, z, x, y):
        directory = self.cache/str(z)/str(x)
        png = directory/(str(y)+'.png')
        try:
            with Image.open(png) as im:
                return im.convert('RGB')
        except (OSError, ValueError):
            pass
        raw = directory/(str(y)+'.pbf')
        try:
            payload = raw.read_bytes()
        except OSError:
            url = self.get_template().format(z=z, x=x, y=y)
            payload = self.fetch(url)
            directory.mkdir(parents=True, exist_ok=True)
            raw.write_bytes(payload)
        image = draw_tile(payload)
        directory.mkdir(parents=True, exist_ok=True)
        image.save(png)
        return image

    def make_view(self, view):
        cx, cy, z = view
        x0, y0 = round(cx-MAP_WIDTH/2), round(cy-MAP_HEIGHT/2)
        image = Image.new('RGB', (MAP_WIDTH, MAP_HEIGHT), (10, 16, 20))
        missing = 0
        n = 2**z
        for ty in range(y0//TILE, (y0+MAP_HEIGHT-1)//TILE+1):
            for tx in range(x0//TILE, (x0+MAP_WIDTH-1)//TILE+1):
                x, y = tx % n, ty
                try:
                    if y < 0 or y >= n:
                        raise OSError('outside map')
                    tile = self.tile(z, x, y)
                    image.paste(tile, (tx*TILE-x0, ty*TILE-y0))
                except Exception as exc:
                    # A malformed cached tile must not kill the worker or block input.
                    print('mesh map tile %s/%s/%s: %s' % (z, x, y, exc), flush=True)
                    missing += 1
        return image, missing

    def run(self):
        while True:
            self.wake.wait()
            self.wake.clear()
            with self.lock:
                view = self.requested
            if view is None:
                continue
            result = self.make_view(view)
            with self.lock:
                if self.requested == view:
                    self.result = (view, *result)
                else:
                    self.wake.set()
