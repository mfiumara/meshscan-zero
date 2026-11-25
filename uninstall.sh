#!/bin/bash
# Remove meshscan. Keeps the database, settings and node identity unless you pass --purge.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
if [ "${1:-}" = "--purge" ]; then
  apt-get purge -y meshscan
  echo "meshscan removed, including its data."
else
  apt-get remove -y meshscan
  echo "meshscan removed. Data kept in /var/lib/meshscan (run with --purge to delete it)."
fi
