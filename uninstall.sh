#!/bin/bash
# Remove meshscan. Keeps the database and node identity unless you pass --purge.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
systemctl disable --now meshscan meshscan-web 2>/dev/null || true
rm -f /etc/systemd/system/meshscan.service /etc/systemd/system/meshscan-web.service /usr/local/bin/meshscan-report
systemctl daemon-reload
rm -rf /opt/meshscan
if [ "${1:-}" = "--purge" ]; then
  rm -rf /var/lib/meshscan /etc/meshscan.json
  echo "meshscan removed, including its data."
else
  echo "meshscan removed. Data kept in /var/lib/meshscan (run with --purge to delete it)."
fi
