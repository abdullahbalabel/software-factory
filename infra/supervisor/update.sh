#!/bin/bash
# Deploy the latest supervisor: pull this repository from its Gitea copy (factory/software-factory, read-only token)
# into /opt/factory/software-factory, provision Gitea for any new product, start the lab-hand runners of products
# that declare `hands: [lab]` (infra/hands/lab), refresh the backup script and its units (the timer's state is kept),
# install the release signer (infra/signer), then install. Run as root on the factory server (infra/factory.sh runs
# it too).
# The workstation pushes the repository to GitHub (origin) and to that Gitea copy first.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
REPO=/opt/factory/software-factory
URL=http://127.0.0.1:3000/factory/software-factory.git
AUTH="Authorization: token $(cat /srv/factory/secrets/supervisor/gitea_token_reader 2>/dev/null ||
  cat /srv/factory/secrets/gitea_ops_token)"
if [ -d "$REPO/.git" ]; then
  git -C "$REPO" -c http.extraHeader="$AUTH" fetch -q origin main
  git -C "$REPO" reset -q --hard origin/main
else
  install -d -m 755 /opt/factory
  git -c http.extraHeader="$AUTH" clone -q "$URL" "$REPO"
fi
chmod -R go-w,a+rX "$REPO"
git -C "$REPO" log -1 --format='deploying %h %s'
python3 "$REPO/infra/supervisor/provision.py"
python3 "$REPO/infra/hands/lab/install.py"
install -d -m 755 /srv/factory/backup
install -m 750 -o root -g root "$REPO/infra/backup/backup.sh" "$REPO/infra/backup/restore-test.sh" /srv/factory/backup/
install -m 644 "$REPO/infra/backup/factory-backup.service" "$REPO/infra/backup/factory-backup.timer" /etc/systemd/system/
bash "$REPO/infra/signer/install.sh"
bash "$REPO/infra/supervisor/install.sh"
