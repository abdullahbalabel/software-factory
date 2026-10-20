#!/bin/bash
# Install or update the factory supervisor on the factory VM. Run as root from the checkout
# /opt/factory/software-factory (infra/supervisor/update.sh pulls it from Gitea and calls this). Idempotent.
#
# Sets up:
#   users      omp-broker (holds the model login), factory-supervisor (runs the service; Zulip/Gitea secrets),
#              agent-chat + agent-<product> (run OMP sessions; no sudo, no docker, no secrets; homes 0700)
#   OMP        one shared, root-owned install in /opt/factory-omp (agents cannot modify it); each agent user has its
#              own OMP home (~/.omp: profiles, sessions, memory)
#   sudo       factory-supervisor may run only /usr/local/sbin/factory-agent-exec, only as the agent users, and the
#              product helper /usr/local/sbin/factory-product-admin as root (/product new|remove; written by its `sync`)
#   /proc      hidepid=invisible (persistent in /etc/fstab); group procview (the supervisor) sees all processes
#   services   factory-omp-broker, factory-supervisor (systemd, restart always, enabled at boot)
# The model login is copied into the broker with import-login.sh (from an export file the owner made, or from the
# factory user's OMP).
# Hold files (written by infra/factory.sh; the units carry the same conditions, so a reboot cannot start them either):
#   /var/lib/factory/hold             neither broker nor supervisor runs (rehearsal restore: the old server is live)
#   /var/lib/factory/hold-supervisor  the supervisor waits (fresh install, until the server is on the tailnet)
set -euo pipefail
REPO=/opt/factory/software-factory
OMP_VERSION=18.10.3
BUN_VERSION=1.4.3
BUN_SRC=${BUN_SRC:-/home/factory/.bun/bin/bun}
HOLD=/var/lib/factory/hold
cd "$REPO"
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }

python3 -c 'import yaml' 2>/dev/null || apt-get install -y python3-yaml

# -- users ------------------------------------------------------------------------------------------------------------
id omp-broker >/dev/null 2>&1 ||
  useradd --system --user-group --home-dir /var/lib/omp-broker --create-home --shell /usr/sbin/nologin omp-broker
chmod 700 /var/lib/omp-broker
id factory-supervisor >/dev/null 2>&1 ||
  useradd --system --user-group --home-dir /var/lib/factory-supervisor --no-create-home --shell /usr/sbin/nologin \
    factory-supervisor

# -- /proc: each user sees only its own processes (hidepid=invisible); group procview (the supervisor, polkit) sees
# all. Keeps one agent user from reading another's command lines and environment. --------------------------------
getent group procview >/dev/null || groupadd --system procview
usermod -aG procview factory-supervisor
id polkitd >/dev/null 2>&1 && usermod -aG procview polkitd
PROC_OPTS="defaults,nosuid,nodev,noexec,hidepid=invisible,gid=procview"
if ! grep -qE '^proc[[:space:]]+/proc[[:space:]]' /etc/fstab; then
  printf 'proc /proc proc %s 0 0\n' "$PROC_OPTS" >> /etc/fstab
fi
mount -o remount,hidepid=invisible,gid="$(getent group procview | cut -d: -f3)" /proc

# -- OMP (shared, read-only for agents) ------------------------------------------------------------------------------
install -d -m 755 /opt/factory-omp/bin
if [ -x "$BUN_SRC" ]; then
  cmp -s "$BUN_SRC" /opt/factory-omp/bin/bun || install -m 755 "$BUN_SRC" /opt/factory-omp/bin/bun
elif [ "$(/opt/factory-omp/bin/bun --version 2>/dev/null)" != "$BUN_VERSION" ]; then
  # A new server has no workstation copy of bun: the pinned release from GitHub.
  t=$(mktemp -d)
  curl -fsSL -o "$t/bun.zip" "https://github.com/oven-sh/bun/releases/download/bun-v$BUN_VERSION/bun-linux-x64.zip"
  python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extract("bun-linux-x64/bun", sys.argv[2])' \
    "$t/bun.zip" "$t"
  install -m 755 "$t/bun-linux-x64/bun" /opt/factory-omp/bin/bun
  rm -rf "$t"
fi
if ! PATH=/opt/factory-omp/bin:$PATH /opt/factory-omp/bin/omp --version </dev/null 2>/dev/null | grep -q "$OMP_VERSION"; then
  BUN_INSTALL=/opt/factory-omp HOME=/root PATH=/opt/factory-omp/bin:$PATH \
    /opt/factory-omp/bin/bun add -g "@oh-my-pi/pi-coding-agent@$OMP_VERSION" </dev/null
fi
chown -R root:root /opt/factory-omp
chmod -R go-w,a+rX /opt/factory-omp

# -- agent wrapper, product helper, agent users and sudo rules -------------------------------------------------------
install -m 755 -o root -g root infra/supervisor/factory-agent-exec /usr/local/sbin/factory-agent-exec
install -m 755 -o root -g root infra/supervisor/factory-product-admin /usr/local/sbin/factory-product-admin
/usr/local/sbin/factory-product-admin sync

# -- services -------------------------------------------------------------------------------------------------------
install -m 644 infra/supervisor/factory-omp-broker.service infra/supervisor/factory-supervisor.service \
  /etc/systemd/system/
systemctl daemon-reload
systemctl enable factory-omp-broker factory-supervisor
if [ -e "$HOLD" ]; then
  echo "held ($(head -1 "$HOLD")): factory-omp-broker and factory-supervisor not started"
  exit 0
fi
systemctl start factory-omp-broker
for _ in $(seq 30); do [ -s /var/lib/omp-broker/.omp/auth-broker.token ] && break; sleep 1; done
install -d -m 750 -o root -g factory-agents /etc/factory
install -m 640 -o root -g factory-agents /var/lib/omp-broker/.omp/auth-broker.token /etc/factory/omp-broker.token

[ -d /srv/factory/secrets/supervisor ] || { echo "run infra/supervisor/provision.py first" >&2; exit 1; }
if [ -e "$HOLD-supervisor" ]; then
  echo "supervisor held ($(head -1 "$HOLD-supervisor"))"
else
  systemctl restart factory-supervisor
fi
systemctl --no-pager --lines=0 status factory-omp-broker factory-supervisor | grep -E '●|Active:' || true
