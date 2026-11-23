#!/bin/bash
# Install meshscan on a CardputerZero v5 with Cap LoRa-1262. Run as: sudo ./install.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
cd "$(dirname "$0")"
apt-get install -y -qq python3-venv python3-smbus2 python3-spidev python3-libgpiod python3-cryptography >/dev/null
mkdir -p /opt/meshscan /var/lib/meshscan
install -m 755 meshscan.py meshscan-web.py meshscan-report.py /opt/meshscan/
install -d /opt/meshscan/web && install -m 644 web/index.html /opt/meshscan/web/
[ -x /opt/meshscan/venv/bin/python ] || python3 -m venv --system-site-packages /opt/meshscan/venv
/opt/meshscan/venv/bin/pip install -q meshtastic   # protobuf definitions for Meshtastic decoding
printf '#!/bin/sh\nexec /opt/meshscan/venv/bin/python /opt/meshscan/meshscan-report.py "$@"\n' > /usr/local/bin/meshscan-report
chmod 755 /usr/local/bin/meshscan-report
install -m 644 systemd/meshscan.service systemd/meshscan-web.service /etc/systemd/system/
# The radio can only be owned by one program.
if systemctl is-enabled --quiet meshtasticd 2>/dev/null; then
  echo "Stopping and disabling meshtasticd (it uses the same radio)."
  systemctl disable --now meshtasticd
fi
systemctl daemon-reload
systemctl enable --now meshscan meshscan-web
echo "Dashboard: http://$(hostname).local:8080"
