#!/bin/bash
# Run on the factory server once it has joined the tailnet (tailscale status shows it online) and the tailnet has
# MagicDNS + HTTPS certificates enabled (Tailscale admin console > DNS). infra/factory.sh runs it after `tailscale up`.
# Publishes the services on the tailnet only:
#   https://<vm>.<tailnet>.ts.net/        Zulip   (Tailscale certificate, Let's Encrypt via Tailscale)
#   https://<vm>.<tailnet>.ts.net:8443/   Gitea
#   ssh://git@<vm>.<tailnet>.ts.net:2222  Gitea SSH
# Nothing is published on the internet (no Funnel). Then points Zulip's EXTERNAL_HOST, Gitea's ROOT_URL and the
# supervisor (/etc/factory-supervisor/site.yml, read over supervisor/config.yml) at the name. Other keys in the .env files stay.
set -euo pipefail
NAME=$(tailscale status --json | jq -r '.Self.DNSName' | sed 's/\.$//')
case "$NAME" in *.ts.net) ;; *) echo "not on a tailnet with MagicDNS: '$NAME'" >&2; exit 1 ;; esac
sudo tailscale serve --bg --https=443 http://127.0.0.1:8080
sudo tailscale serve --bg --https=8443 http://127.0.0.1:3000
sudo tailscale serve --bg --tcp=2222 tcp://127.0.0.1:2222
sudo tailscale serve status

setenv() { # file key value: replace or append one KEY=value line
  local f=$1 k=$2 v=$3
  sudo touch "$f"
  sudo sed -i "/^$k=/d" "$f"
  printf '%s=%s\n' "$k" "$v" | sudo tee -a "$f" >/dev/null
}
setenv /srv/factory/zulip/.env ZULIP_EXTERNAL_HOST "$NAME"
setenv /srv/factory/gitea/.env GITEA_DOMAIN "$NAME"
setenv /srv/factory/gitea/.env GITEA_ROOT_URL "https://$NAME:8443/"
sudo install -d -m 755 /etc/factory-supervisor
printf 'zulip:\n  site: https://%s\ngitea:\n  public: https://%s:8443\n' "$NAME" "$NAME" |
  sudo tee /etc/factory-supervisor/site.yml >/dev/null
(cd /srv/factory/zulip && sudo docker compose up -d --wait zulip)
(cd /srv/factory/gitea && sudo docker compose up -d gitea)
echo "Zulip: https://$NAME/   Gitea: https://$NAME:8443/"
