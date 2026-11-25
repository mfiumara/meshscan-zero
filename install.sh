#!/bin/bash
# Build the meshscan .deb from this checkout and install it (developer path).
# Normal users: install "Mesh Map" from the CardputerZero AppStore instead.
#
#   sudo ./install.sh
#
# Upgrading from an older install.sh install keeps your database, settings, PIN and node identity.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
cd "$(dirname "$0")"
command -v dpkg-deb >/dev/null || { echo "dpkg-deb not found"; exit 1; }
out=$(sudo -u "${SUDO_USER:-root}" packaging/build-deb.sh)
deb=$(printf '%s\n' "$out" | head -n 1)
echo "built $deb"
apt-get install -y "$deb"
IP=$(hostname -I | awk '{print $1}')
echo
echo "meshscan is running. Open http://$(hostname).local:8080 (or http://$IP:8080)."
echo "Mesh Map is in the launcher (restart APPLaunch or log in again if it is not listed yet)."
