#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-or-later
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run with sudo' >&2; exit 1; }
HERE=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
install -d /opt/meshscan-radar /usr/share/APPLaunch/applications /usr/share/APPLaunch/share/images
install -m 755 "$HERE/radar.py" /opt/meshscan-radar/radar.py
if [ ! -x /opt/meshscan-radar/venv/bin/python ]; then
    python3 -m venv --system-site-packages /opt/meshscan-radar/venv
fi
/opt/meshscan-radar/venv/bin/python -m pip install 'qrcode>=7.4,<9'
/opt/meshscan-radar/venv/bin/python -c 'import PIL, numpy' || /opt/meshscan-radar/venv/bin/python -m pip install pillow numpy
install -m 644 "$HERE/meshscan-radar.desktop" /usr/share/APPLaunch/applications/meshscan-radar.desktop
install -m 644 "$HERE/meshscan-radar.png" /usr/share/APPLaunch/share/images/meshscan-radar.png
echo 'Installed Mesh Radar; open it from APPLaunch.'
