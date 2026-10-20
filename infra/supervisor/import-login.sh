#!/bin/bash
# Copy the owner's model login into the auth broker's store. Run as root on the factory server.
#   import-login.sh <file>  from an export the owner made on his laptop (`omp auth export > factory-login.json`)
#                           and copied to the server; the file is deleted afterwards
#   import-login.sh         from the factory user's OMP, where the owner imported it on the first server
# The credential passes through a pipe only. Afterwards the broker is the only process that uses and refreshes it:
# do not run model work with any other OMP holding the same login (two holders refreshing one login can invalidate
# each other).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
[ ! -e /var/lib/factory/hold ] || { echo "this server is held ($(head -1 /var/lib/factory/hold)); no login here" >&2; exit 1; }
FILE=${1:-}
systemctl stop factory-omp-broker 2>/dev/null || true
import() {
  sudo -u omp-broker env HOME=/var/lib/omp-broker PATH=/opt/factory-omp/bin:/usr/bin:/bin \
    /opt/factory-omp/bin/omp auth import - >/dev/null
}
if [ -n "$FILE" ]; then
  import < "$FILE"
  shred -u "$FILE" 2>/dev/null || rm -f "$FILE"
else
  sudo -u factory -H bash -c 'PATH=$HOME/.bun/bin:$PATH omp auth export </dev/null' | import
fi
systemctl start factory-omp-broker
echo "login imported into the broker"
