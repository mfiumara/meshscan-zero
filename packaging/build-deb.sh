#!/bin/sh
# Build dist/meshscan_<version>_all.deb from this checkout.
#
#   packaging/build-deb.sh            # version from app-builder.json
#   packaging/build-deb.sh 1.0.1      # override
#
# Needs dpkg-deb (macOS: `brew install dpkg`), python3 and curl. Reproducible: file times are
# clamped to the last commit, owners are root:root and the only download (openhop-core) is
# pinned by sha256 in packaging/vendor.lock.
set -eu
cd "$(dirname "$0")/.."
ROOT=$(pwd)
VERSION=${1:-$(python3 -c 'import json; print(json.load(open("app-builder.json"))["version"])')}
SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(git log -1 --format=%ct 2>/dev/null || date +%s)}
export SOURCE_DATE_EPOCH
BUILD="$ROOT/build/deb"
PKG="$BUILD/root"
CACHE="$ROOT/build/cache"
OUT="$ROOT/dist/meshscan_${VERSION}_all.deb"

rm -rf "$BUILD"
mkdir -p "$PKG/DEBIAN" "$CACHE" "$ROOT/dist"
LIB="$PKG/usr/lib/meshscan"
mkdir -p "$LIB/web" "$LIB/vendor" "$LIB/map/fonts" \
         "$PKG/usr/lib/systemd/system" "$PKG/usr/bin" \
         "$PKG/usr/share/APPLaunch/applications" "$PKG/usr/share/APPLaunch/share/images" \
         "$PKG/usr/share/doc/meshscan/licenses"

# App code.
for f in meshscan.py meshscan_chat.py meshscan_board.py meshscan_common.py meshscan_pb.py meshscan-web.py meshscan-report.py; do
    install -m 644 "$f" "$LIB/$f"
done
install -m 644 web/index.html "$LIB/web/index.html"
install -m 755 packaging/debian/meshscan-map "$LIB/meshscan-map"
for f in radar.py chat.py map_tiles.py; do install -m 644 "device/$f" "$LIB/map/$f"; done
install -m 644 device/fonts/Inter.ttf device/fonts/JetBrainsMono.ttf "$LIB/map/fonts/"

# Vendored pure-Python dependencies, verified against the lock file.
grep -v '^#' packaging/vendor.lock | while read -r name version sha url; do
    [ -n "$name" ] || continue
    whl="$CACHE/$(basename "$url")"
    [ -f "$whl" ] || curl -fsSL "$url" -o "$whl"
    got=$(python3 -c 'import hashlib,sys; print(hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$whl")
    [ "$got" = "$sha" ] || { echo "sha256 mismatch for $name: $got" >&2; exit 1; }
    python3 - "$whl" "$LIB/vendor" "$PKG/usr/share/doc/meshscan/licenses" "$name" <<'PY'
import sys, zipfile, os
whl, dest, lic, name = sys.argv[1:]
with zipfile.ZipFile(whl) as z:
    for n in z.namelist():
        if ".dist-info/" in n:
            if "/licenses/" in n or n.endswith("/LICENSE"):
                with open(os.path.join(lic, f"{name}-LICENSE"), "wb") as f:
                    f.write(z.read(n))
            continue
        if "__pycache__" in n or n.endswith("/"):
            continue
        z.extract(n, dest)
PY
done
cp -R packaging/shim/Crypto "$LIB/vendor/Crypto"

# System integration.
install -m 644 packaging/debian/meshscan.service packaging/debian/meshscan-web.service "$PKG/usr/lib/systemd/system/"
install -m 755 packaging/debian/meshscan-report "$PKG/usr/bin/meshscan-report"
install -m 644 packaging/debian/meshscan.desktop "$PKG/usr/share/APPLaunch/applications/meshscan.desktop"
install -m 644 device/meshscan-radar.png "$PKG/usr/share/APPLaunch/share/images/meshscan.png"

# Docs and licences.
install -m 644 packaging/debian/copyright NOTICE README.md "$PKG/usr/share/doc/meshscan/"
install -m 644 LICENSE "$PKG/usr/share/doc/meshscan/licenses/GPL-3.0.txt"
install -m 644 licenses/Apache-2.0.txt "$PKG/usr/share/doc/meshscan/licenses/Apache-2.0.txt"
install -m 644 device/fonts/Inter-OFL.txt device/fonts/JetBrainsMono-OFL.txt "$PKG/usr/share/doc/meshscan/licenses/"

# Control files.
find "$PKG" -name __pycache__ -prune -exec rm -rf {} +
find "$PKG" -name '.DS_Store' -delete
for s in preinst postinst prerm postrm; do install -m 755 "packaging/debian/$s" "$PKG/DEBIAN/$s"; done
SIZE=$(du -sk "$PKG/usr" | cut -f1)
sed -e "s/@VERSION@/$VERSION/" -e "s/@SIZE@/$SIZE/" packaging/debian/control.in > "$PKG/DEBIAN/control"
chmod 644 "$PKG/DEBIAN/control"
find "$PKG" -type d -exec chmod 755 {} +
find "$PKG" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} + 2>/dev/null || \
    find "$PKG" -exec touch -h -t "$(date -r "$SOURCE_DATE_EPOCH" +%Y%m%d%H%M.%S)" {} +

dpkg-deb --root-owner-group -Zxz --build "$PKG" "$OUT" >/dev/null
echo "$OUT"
python3 -c 'import hashlib,sys; print("sha256", hashlib.sha256(open(sys.argv[1],"rb").read()).hexdigest())' "$OUT"
