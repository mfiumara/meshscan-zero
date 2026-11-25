"""Draw the Mesh Map icon in the launcher's style: bold, flat, saturated shapes on black.

python3 packaging/make-icon.py  ->  store/icon.png (256 px) and device/meshscan-radar.png (100 px launcher icon)
"""
import math, os
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = 1024  # draw large, downsample for clean anti-aliasing
BLUE, LIGHT, GREEN, WHITE = (94, 177, 255), (170, 215, 255), (79, 220, 152), (240, 246, 252)


def icon(size):
    im = Image.new("RGB", (S, S), (0, 0, 0))
    d = ImageDraw.Draw(im)
    # Folded map: three panels, outer ones lighter, thick light outline.
    x0, x1, x2, x3 = 110, 375, 649, 914
    top, bot, lift = 250, 860, 70
    panels = [
        [(x0, top + lift), (x1, top), (x1, bot - lift), (x0, bot)],
        [(x1, top), (x2, top + lift), (x2, bot), (x1, bot - lift)],
        [(x2, top + lift), (x3, top), (x3, bot - lift), (x2, bot)],
    ]
    for poly, fill in zip(panels, [(38, 104, 184), (27, 78, 146), (38, 104, 184)]):
        d.polygon(poly, fill=fill)
    for poly in panels:
        d.line(poly + [poly[0]], fill=LIGHT, width=26, joint="curve")
    # Mesh: three nodes and their links across the map.
    nodes = [(235, 700), (520, 760), (790, 610)]
    for a, b in zip(nodes, nodes[1:]):
        d.line([a, b], fill=GREEN, width=22)
    for x, y in nodes:
        d.ellipse((x - 52, y - 52, x + 52, y + 52), fill=GREEN, outline=(14, 40, 30), width=14)
    # Location pin with a dark rim so it separates from the map.
    cx, cy, r = 512, 395, 150
    tip = (cx, 690)
    ang = math.asin(r / (tip[1] - cy))
    left = (cx - r * math.cos(ang), cy + r * math.sin(ang))
    right = (cx + r * math.cos(ang), cy + r * math.sin(ang))
    for grow, col in ((18, (10, 16, 24)), (0, WHITE)):
        d.polygon([(left[0] - grow, left[1]), (tip[0], tip[1] + grow), (right[0] + grow, right[1])], fill=col)
        d.ellipse((cx - r - grow, cy - r - grow, cx + r + grow, cy + r + grow), fill=col)
    d.ellipse((cx - 62, cy - 62, cx + 62, cy + 62), fill=BLUE)
    return im.resize((size, size), Image.Resampling.LANCZOS)


if __name__ == "__main__":
    os.makedirs(os.path.join(ROOT, "store"), exist_ok=True)
    icon(256).save(os.path.join(ROOT, "store", "icon.png"))
    # The launcher shows icons on black and expects an opaque 100 px RGB image.
    icon(100).save(os.path.join(ROOT, "device", "meshscan-radar.png"))
    print("wrote store/icon.png and device/meshscan-radar.png")
