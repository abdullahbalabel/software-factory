#!/bin/bash
# Install or update the factory signer (D22; infra/signer/factory-signer). Run as root from the checkout
# /opt/factory/software-factory: infra/supervisor/update.sh calls it, so every deploy, `factory.sh install` (empty
# product list) and `factory.sh restore` (products and keys from the backup, /srv/factory) sets it up. Idempotent.
#
#   user     factory-signer: system user, no login shell, not in the docker group; owns /srv/factory/signer (0700:
#            keys, Gitea token, state). Agents and the supervisor cannot read it; /proc is hidepid (install.sh).
#   tools    apksigner, zipalign, aapt (Ubuntu packages; Java comes with apksigner)
#   sudo     factory-signer may run only `/usr/local/sbin/factory-signer fetch …` as root: reading an artifact out of
#            Gitea's storage needs Docker (the database) and the Docker volume, which the user must not have.
#   units    factory-signer.service (oneshot, as factory-signer) and factory-signer.timer (every 5 minutes); held
#            like the supervisor on a rehearsal restore or a stopped server (/var/lib/factory/hold).
#   Gitea    bot factory-signer, its token, its access to every configured repository (factory-signer provision).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
REPO=/opt/factory/software-factory
cd "$REPO"

dpkg -s apksigner zipalign aapt >/dev/null 2>&1 ||
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq apksigner zipalign aapt >/dev/null

id factory-signer >/dev/null 2>&1 ||
  useradd --system --user-group --home-dir /srv/factory/signer --no-create-home --shell /usr/sbin/nologin \
    factory-signer
install -d -m 700 -o factory-signer -g factory-signer /srv/factory/signer

install -m 755 -o root -g root infra/signer/factory-signer /usr/local/sbin/factory-signer
t=$(mktemp)
echo 'factory-signer ALL=(root) NOPASSWD: /usr/local/sbin/factory-signer fetch *' > "$t"
visudo -cqf "$t"
install -m 440 -o root -g root "$t" /etc/sudoers.d/factory-signer
rm -f "$t"

python3 /usr/local/sbin/factory-signer provision

install -m 644 infra/signer/factory-signer.service infra/signer/factory-signer.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now factory-signer.timer >/dev/null 2>&1
echo "signer: $(grep -c '^- repo:' /srv/factory/signer.yml || true) product(s); timer $(systemctl is-active factory-signer.timer)"
