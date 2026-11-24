#!/bin/bash
# Install meshscan on a CardputerZero (v5) with the Cap LoRa-1262.
#   git clone https://github.com/mfiumara/meshscan-zero && cd meshscan-zero && sudo ./install.sh
# Then open http://<cardputer>.local:8080 and finish the setup in the browser.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
cd "$(dirname "$0")"
USER_NAME="${SUDO_USER:-pi}"

echo "==> system packages"
apt-get update -qq || echo "    (apt update failed; trying with the existing package lists)"
apt-get install -y -qq python3-venv python3-smbus2 python3-spidev python3-libgpiod python3-cryptography >/dev/null

echo "==> files"
install -d /opt/meshscan /opt/meshscan/web /var/lib/meshscan
install -m 755 meshscan.py meshscan_chat.py meshscan_board.py meshscan_common.py meshscan-web.py meshscan-report.py /opt/meshscan/
install -m 644 web/index.html /opt/meshscan/web/
# The dashboard runs as the desktop user and drops settings, mode switches and outgoing messages here.
install -d -o "$USER_NAME" -g "$USER_NAME" -m 755 /var/lib/meshscan/ctl /var/lib/meshscan/ctl/outbox

echo "==> python environment (openhop-core for chat mode, meshtastic protobufs for decoding)"
[ -x /opt/meshscan/venv/bin/python ] || python3 -m venv --system-site-packages /opt/meshscan/venv
/opt/meshscan/venv/bin/pip install -q --upgrade openhop-core python-periphery meshtastic
/opt/meshscan/venv/bin/python -c "import openhop_core, periphery, meshtastic.protobuf.mesh_pb2" \
  || { echo "python dependencies failed to import"; exit 1; }

printf '#!/bin/sh\nexec /opt/meshscan/venv/bin/python /opt/meshscan/meshscan-report.py "$@"\n' > /usr/local/bin/meshscan-report
chmod 755 /usr/local/bin/meshscan-report

echo "==> services"
sed "s/^User=pi$/User=$USER_NAME/" systemd/meshscan-web.service > /etc/systemd/system/meshscan-web.service
install -m 644 systemd/meshscan.service /etc/systemd/system/
# The radio can only be owned by one program.
if systemctl is-enabled --quiet meshtasticd 2>/dev/null; then
  echo "    stopping and disabling meshtasticd (it uses the same radio)"
  systemctl disable --now meshtasticd
fi
systemctl daemon-reload
systemctl enable meshscan meshscan-web >/dev/null
systemctl restart meshscan meshscan-web

for i in $(seq 1 20); do curl -fs localhost:8080/api/settings >/dev/null 2>&1 && break; sleep 1; done
IP=$(hostname -I | awk '{print $1}')
echo
echo "meshscan is running. Open http://$(hostname).local:8080 (or http://$IP:8080) to finish setup."
