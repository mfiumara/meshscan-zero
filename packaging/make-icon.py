"""Draw the Mesh Map icon: a folded dark map with a location pin and two mesh nodes.

python3 packaging/make-icon.py  ->  store/icon.png (256 px) and device/meshscan-radar.png (100 px launcher icon)
"""
import math, os
from PIL import Image, ImageDraw, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = 1024  # draw large, downsample for clean anti-aliasing
BLUE, GREEN = (94, 177, 255), (79, 220, 152)


def icon(size):
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    # Tile: near-black rounded square with a hairline rim, like the launcher's other icons.
    d.rounded_rectangle((16, 16, S - 16, S - 16), radius=210, fill=(12, 18, 24), outline=(44, 66, 76), width=10)
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((150, 120, 870, 840), fill=(40, 120, 170, 70))
    im.alpha_composite(glow.filter(ImageFilter.GaussianBlur(120)))
    d = ImageDraw.Draw(im)
    # Folded map: three panels, the middle one slightly darker, with a fold shadow.
    x0, x1, x2, x3 = 160, 390, 634, 864
    top, bot, lift = 270, 800, 40
    panels = [
        [(x0, top + lift), (x1, top), (x1, bot - lift), (x0, bot)],
        [(x1, top), (x2, top + lift), (x2, bot), (x1, bot - lift)],
        [(x2, top + lift), (x3, top), (x3, bot - lift), (x2, bot)],
    ]
    for poly, fill in zip(panels, [(24, 44, 54), (19, 36, 45), (24, 44, 54)]):
        d.polygon(poly, fill=fill)
    for poly in panels:
        d.line(poly + [poly[0]], fill=(86, 128, 140), width=12, joint="curve")
    # Roads on the map.
    d.line([(x0 + 20, 680), (500, 590), (720, 640), (x3 - 20, 540)], fill=(64, 96, 106), width=10, joint="curve")
    d.line([(520, top + 70), (560, 480), (530, bot - 90)], fill=(52, 80, 90), width=8, joint="curve")
    # Mesh link between the two nodes.
    a, b = (270, 650), (750, 590)
    d.line([a, b], fill=(94, 177, 255, 110), width=6)
    for (x, y), c in [(a, GREEN), (b, BLUE)]:
        halo = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(halo).ellipse((x - 70, y - 70, x + 70, y + 70), fill=c + (120,))
        im.alpha_composite(halo.filter(ImageFilter.GaussianBlur(30)))
        d = ImageDraw.Draw(im)
        d.ellipse((x - 34, y - 34, x + 34, y + 34), fill=c)
    # Location pin.
    cx, cy, r = 512, 400, 108
    tip = (cx, 640)
    ang = math.asin(r / (tip[1] - cy))
    left = (cx - r * math.cos(ang), cy + r * math.sin(ang))
    right = (cx + r * math.cos(ang), cy + r * math.sin(ang))
    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse((cx - 64, 625, cx + 64, 655), fill=(0, 0, 0, 160))
    im.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(10)))
    d = ImageDraw.Draw(im)
    d.polygon([left, tip, right], fill=BLUE)
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=BLUE)
    d.ellipse((cx - 42, cy - 42, cx + 42, cy + 42), fill=(12, 18, 24))
    out = im.resize((size, size), Image.Resampling.LANCZOS)
    return out


if __name__ == "__main__":
    os.makedirs(os.path.join(ROOT, "store"), exist_ok=True)
    icon(256).save(os.path.join(ROOT, "store", "icon.png"))
    # The launcher draws icons on a dark tile; it expects an opaque 100 px RGB image.
    small = icon(100)
    bg = Image.new("RGB", small.size, (0, 0, 0))
    bg.paste(small, mask=small.split()[3])
    bg.save(os.path.join(ROOT, "device", "meshscan-radar.png"))
    print("wrote store/icon.png and device/meshscan-radar.png")
