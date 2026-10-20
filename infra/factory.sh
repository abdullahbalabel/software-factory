#!/bin/bash
# The factory on any server (F6). Run as root on a fresh Ubuntu 24.04 server (a VM on a KVM host such as the one
# infra/host/mk-factory-vm.sh makes, or a plain cloud server), from a git checkout of this repository.
# Owner guide: README.md, "Move or reinstall the factory".
#
#   factory.sh install
#       A new, empty factory: Docker, Tailscale (installed, not joined), restic, the server firewall
#       (infra/core/factory-host.nft), Zulip (organisation, owner account without a password, supervisor bot,
#       streams), Gitea + PostgreSQL + Actions runner (no Docker socket in jobs), the factory's Gitea projects
#       (provision.py; this checkout pushed as factory/software-factory), OMP, auth broker, agent users, helpers, sudo
#       rules, /proc hidepid, nightly backup, the release signer (infra/signer, no products yet). Idempotent. The
#       supervisor waits for the tailnet (`tailnet`). Ends with the owner's remaining steps; prints no secret.
#   factory.sh restore --from <repository> --password-file <file> [--snapshot <id>] [--take-over [--old-is-gone]]
#       Everything `install` sets up, then the factory from a restic backup (infra/backup/backup.sh): /srv/factory
#       (secrets, compose files, workspaces), Gitea (database dump + data volume), Zulip (data volume + its dump),
#       the products and runners (from the restored Gitea copy of this repository), agent users and their OMP homes,
#       the supervisor's state, the release signer's products and keys (/srv/factory/signer.yml, /srv/factory/signer;
#       held like the supervisor on a rehearsal). <repository>: a local restic repository (an off-site
#       copy of the backup) or [sftp:][user@]host:/path, fetched over SSH with rsync; it becomes this server's
#       backup repository.
#       Without --take-over (rehearsal, safe while the old server runs): the broker's model login is not restored and
#       any login copy in the agents' OMP homes is deleted; broker and supervisor are held (they do not start, not
#       even after a reboot); Zulip's push registration is removed from the copy and push is off; no tailnet, no
#       nightly backup. Nothing on this server can act as the live factory.
#       With --take-over (the old server is stopped: `factory.sh stop` there wrote a snapshot tagged `final`; if the
#       old server is lost, add --old-is-gone): also the model login and the push registration, then the tailnet
#       under the old name (approval link), Zulip and Gitea published, nightly backup, broker and supervisor started.
#   factory.sh stop
#       On the OLD server when moving: stops the supervisor and the broker, writes the final backup (tag `final`),
#       stops Zulip, Gitea and the runners, the nightly backup and Tailscale (logout). Held afterwards.
#   factory.sh tailnet [--hostname <name>]
#       Join the tailnet (prints the approval link and a QR code; waits), publish Zulip and Gitea on it
#       (infra/core/tailnet-serve.sh), start the supervisor. Default name: the restored one, else factory-core.
#   factory.sh owner-link            one-time link for the owner's Zulip password (open it with Tailscale on)
#   factory.sh push-register         register with Zulip's push service (only after the owner accepted its terms)
#   factory.sh import-login <file>   the owner's model login export (omp auth export) into the broker; deletes it
set -euo pipefail
SRC=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
REPO=/opt/factory/software-factory
F=/srv/factory
S=$F/secrets
STATE=/var/lib/factory
HOLD=$STATE/hold
BACKUP_REPO=/srv/factory-backup/restic
WORK=/srv/factory-restore
VOL=/var/lib/docker/volumes
GITEA=http://127.0.0.1:3000
OWNER_EMAIL=${FACTORY_OWNER_EMAIL:-}            # the owner's Zulip sign-in (needed by install)
OWNER_NAME=${FACTORY_OWNER_NAME:-Owner}
REALM_NAME=${FACTORY_REALM_NAME:-"Software Factory"}
BOT_EMAIL=supervisor-bot@factory-core.tailnet.invalid
export DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1

T0=$SECONDS
log() { printf '%s +%ss  %s\n' "$(date -u +%T)" "$((SECONDS - T0))" "$*"; }
die() { echo "factory.sh: $*" >&2; exit 1; }
zc() { (cd "$F/zulip" && docker compose --progress quiet "$@"); }
gc() { (cd "$F/gitea" && docker compose --progress quiet "$@"); }
gx() { docker exec -u git factory-gitea-gitea-1 gitea "$@"; }
setenv() { # file key value: replace or append one KEY=value line, other lines stay
  touch "$1"; sed -i "/^$2=/d" "$1"; printf '%s=%s\n' "$2" "$3" >> "$1"
}
role() { cat "$STATE/role" 2>/dev/null || echo none; }
set_role() { install -d -m 755 "$STATE"; echo "$1" > "$STATE/role"; }
hold() { install -d -m 755 "$STATE"; printf '%s\n' "$2" > "$1"; }

# -- building blocks ------------------------------------------------------------------------------------------------
packages() {
  . /etc/os-release
  [ "$ID $VERSION_ID" = "ubuntu 24.04" ] || die "needs Ubuntu 24.04 (this is $PRETTY_NAME)"
  log "packages"
  apt-get update -qq
  apt-get install -y -qq ca-certificates curl git jq nftables python3-yaml restic rsync >/dev/null
  if ! docker compose version >/dev/null 2>&1; then
    log "Docker (download.docker.com)"
    install -m 755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu noble stable" \
      > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin >/dev/null
  fi
  systemctl enable --now docker >/dev/null 2>&1
  if ! command -v tailscale >/dev/null; then
    log "Tailscale (pkgs.tailscale.com; not joined)"
    curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg \
      -o /usr/share/keyrings/tailscale-archive-keyring.gpg
    curl -fsSL https://pkgs.tailscale.com/stable/ubuntu/noble.tailscale-keyring.list \
      -o /etc/apt/sources.list.d/tailscale.list
    apt-get update -qq
    apt-get install -y -qq tailscale >/dev/null
  fi
  systemctl enable --now tailscaled >/dev/null 2>&1
}

firewall() {
  install -d -m 755 /usr/local/lib/factory
  install -m 644 "$SRC/infra/core/factory-host.nft" /usr/local/lib/factory/factory-host.nft
  install -m 644 "$SRC/infra/core/factory-firewall.service" /etc/systemd/system/
  systemctl daemon-reload
  systemctl enable factory-firewall >/dev/null 2>&1
  systemctl restart factory-firewall
  log "firewall: table inet factory_host (inbound: SSH, Tailscale, ICMP, DHCP; services on loopback only)"
}

files() { # $1: checkout to take the compose files and scripts from
  local c=$1
  install -d -m 755 "$F" "$F/zulip" "$F/gitea" "$F/backup" "$F/workspaces"
  install -m 644 "$c/infra/core/zulip/compose.yaml" "$c/infra/core/zulip/compose.override.yaml" "$F/zulip/"
  install -m 755 "$c/infra/core/zulip/owner-link.sh" "$F/zulip/"
  install -m 644 "$c/infra/core/gitea/compose.yaml" "$c/infra/core/gitea/runner.yaml" "$F/gitea/"
  install -m 755 "$c/infra/core/gen-secrets.sh" "$c/infra/core/tailnet-serve.sh" "$F/"
  install -m 750 "$c/infra/backup/backup.sh" "$c/infra/backup/restore-test.sh" "$F/backup/"
  install -m 644 "$c/infra/backup/factory-backup.service" "$c/infra/backup/factory-backup.timer" /etc/systemd/system/
  systemctl daemon-reload
}

wait_gitea() {
  for _ in $(seq 120); do curl -sf "$GITEA/api/healthz" >/dev/null && return 0; sleep 2; done
  die "Gitea did not become healthy"
}

zulip_up() {
  log "Zulip: start (first start sets up its database; a few minutes)"
  zc up -d --quiet-pull --wait --wait-timeout 1200 >/dev/null
}

zulip_bootstrap() { # organisation, owner, bot, streams (fresh factory)
  local streams out
  streams=$(python3 - "$SRC" <<'EOF'
import glob, json, os, sys, yaml
s = [["factory", "Daily digest, factory health, pending decisions across products"]]
for pf in sorted(glob.glob(os.path.join(sys.argv[1], "products", "*", "product.yml"))):
    p = yaml.safe_load(open(pf))
    s.append([p["zulip_stream"], p.get("title", p["name"])[:60]])
print(json.dumps(s))
EOF
)
  out=$(zc exec -T -u zulip -e FACTORY_REALM_NAME="$REALM_NAME" -e FACTORY_OWNER_EMAIL="$OWNER_EMAIL" \
    -e FACTORY_OWNER_NAME="$OWNER_NAME" -e FACTORY_BOT_EMAIL="$BOT_EMAIL" -e FACTORY_STREAMS="$streams" \
    zulip /home/zulip/deployments/current/manage.py shell < "$SRC/infra/core/zulip/bootstrap-realm.py" \
    2>"$STATE/zulip-bootstrap.err")
  grep -v '^BOT_API_KEY=' <<<"$out" | grep -E '^created' | sed 's/^/  zulip: /' || true
  local key
  key=$(sed -n 's/^BOT_API_KEY=//p' <<<"$out")
  [ -n "$key" ] || die "Zulip bootstrap did not return the bot's key"
  (umask 022; printf '%s' "$key" > "$S/zulip_supervisor_bot_api_key")
  log "Zulip: organisation '$REALM_NAME', owner $OWNER_EMAIL (no password yet), supervisor bot, streams"
}

gitea_bootstrap() { # owner (must change password), factory-ops + token, runner registration token (fresh factory)
  local users
  users=$(gx admin user list | awk 'NR>1 {print $2}')
  if ! grep -qx owner <<<"$users"; then
    gx admin user create --admin --username owner --email owner@factory.invalid \
      --password "$(cat "$S/gitea_owner_password")" --must-change-password >/dev/null
  fi
  grep -qx factory-ops <<<"$users" ||
    gx admin user create --admin --username factory-ops --email factory-ops@factory.invalid --random-password \
      --must-change-password=false >/dev/null
  if [ ! -s "$S/gitea_ops_token" ]; then
    (umask 022; gx admin user generate-access-token --username factory-ops --token-name factory-ops --scopes all --raw |
      tail -1 | tr -d '\n' > "$S/gitea_ops_token")
  fi
  if [ ! -s "$S/gitea_runner_token" ]; then
    (umask 022; gx actions generate-runner-token | tail -1 | tr -d '\n' > "$S/gitea_runner_token")
  fi
  log "Gitea: accounts owner (password to change at first sign-in) and factory-ops, runner token"
}

publish_checkout() { # this checkout becomes the Gitea copy factory/software-factory (when that is still empty)
  [ -d "$SRC/.git" ] || die "$SRC is not a git checkout (copy the whole repository folder, with .git)"
  local auth
  auth="Authorization: token $(cat "$S/gitea_ops_token")"
  if [ -z "$(git -c safe.directory='*' -c http.extraHeader="$auth" ls-remote "$GITEA/factory/software-factory.git" refs/heads/main)" ]; then
    git -C "$SRC" -c safe.directory='*' -c http.extraHeader="$auth" push -q "$GITEA/factory/software-factory.git" \
      HEAD:refs/heads/main
    log "Gitea: factory/software-factory = this checkout ($(git -C "$SRC" -c safe.directory='*' log -1 --format=%h))"
  fi
}

sync_copy() { # restore: the restored Gitea copy of this repository is fast-forwarded to this checkout when it is newer;
  # otherwise the restored copy (with commits this checkout lacks, e.g. products registered from Zulip) is deployed,
  # but only if it knows the hold files (it must never start the broker or the supervisor of a rehearsal).
  local auth t theirs ours
  auth="Authorization: token $(cat "$S/gitea_ops_token")"
  t=$(mktemp -d)
  git -c http.extraHeader="$auth" clone -q --bare "$GITEA/factory/software-factory.git" "$t/copy.git"
  theirs=$(git -C "$t/copy.git" rev-parse main)
  ours=$(git -C "$SRC" -c safe.directory='*' rev-parse HEAD)
  if git -C "$SRC" -c safe.directory='*' merge-base --is-ancestor "$theirs" "$ours" 2>/dev/null; then
    if [ "$theirs" != "$ours" ]; then
      git -C "$SRC" -c safe.directory='*' -c http.extraHeader="$auth" push -q \
        "$GITEA/factory/software-factory.git" "$ours:refs/heads/main"
      log "Gitea copy of this repository: fast-forwarded to this checkout (${ours:0:8})"
    fi
  else
    git -C "$t/copy.git" show main:infra/supervisor/install.sh | grep -q hold-supervisor ||
      die "the restored copy of this repository predates infra/factory.sh holds, and this checkout does not contain it; restore with a checkout that does"
    log "Gitea copy of this repository: restored one kept (${theirs:0:8}; it has commits this checkout lacks)"
  fi
  rm -rf "$t"
}

deploy() { # the deployed checkout from the Gitea copy, Gitea provisioning, lab runners, supervisor install (held or not)
  log "supervisor, broker, agents: infra/supervisor/update.sh"
  bash "$SRC/infra/supervisor/update.sh" 2>&1 | sed 's/^/  /'
  files "$REPO"
}

backup_timer() {
  systemctl enable --now factory-backup.timer >/dev/null 2>&1
  log "nightly backup: factory-backup.timer enabled ($BACKUP_REPO)"
}

# -- install --------------------------------------------------------------------------------------------------------
cmd_install() {
  [ -n "$OWNER_EMAIL" ] || die "set FACTORY_OWNER_EMAIL to the owner's e-mail address (his Zulip sign-in)"
  if [ "$(role)" = rehearsal ] || [ "$(role)" = live ] || [ "$(role)" = retired ]; then
    die "this server holds a restored factory (role $(role)); install is for new servers"
  fi
  if [ "$(role)" = none ] && docker volume inspect factory-gitea_gitea-data >/dev/null 2>&1; then
    die "this server already runs a factory that this script did not install; nothing changed"
  fi
  packages
  firewall
  files "$SRC"
  log "secrets (generated once, kept)"
  bash "$F/gen-secrets.sh" | sed 's/^/  /'
  [ -f "$F/zulip/.env" ] || setenv "$F/zulip/.env" ZULIP_PUSH_NOTIFICATIONS False
  grep -q '^ZULIP_ADMINISTRATOR=' "$F/zulip/.env" || setenv "$F/zulip/.env" ZULIP_ADMINISTRATOR "$OWNER_EMAIL"
  set_role fresh
  hold "$HOLD-supervisor" "fresh install: the supervisor starts when the server is on the tailnet (factory.sh tailnet)"
  zulip_up
  zulip_bootstrap
  log "Gitea: start"
  gc up -d --quiet-pull --wait gitea-db gitea >/dev/null
  wait_gitea
  gitea_bootstrap
  gc up -d --quiet-pull runner >/dev/null
  python3 "$SRC/infra/supervisor/provision.py" | sed 's/^/  /'
  publish_checkout
  deploy
  backup_timer
  report_health
  cat <<EOF

The factory is installed (an empty factory: no tasks, no messages). The owner's remaining steps, in this order:
  1. Tailnet:  sudo bash $REPO/infra/factory.sh tailnet --hostname factory-core
               shows a link and a QR code: open it on your phone, sign in to Tailscale, approve the server.
               (Tailscale admin console > DNS: MagicDNS and HTTPS certificates on.) The supervisor starts then.
  2. Zulip:    sudo bash $REPO/infra/factory.sh owner-link
               open the link on your phone with Tailscale on, choose your password, then turn on two-factor.
  3. Push:     only after you have read and accepted Zulip's terms (https://zulip.com/policies/terms):
               sudo bash $REPO/infra/factory.sh push-register
  4. Model:    on your laptop: omp auth export > factory-login.json ; copy the file to this server, then
               sudo bash $REPO/infra/factory.sh import-login factory-login.json
  5. Phone:    Zulip app > add organisation > https://<the name step 1 shows>/
EOF
}

report_health() {
  local z g
  local host
  host=$(sed -n 's/^ZULIP_EXTERNAL_HOST=//p' "$F/zulip/.env" | tail -1)
  z=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: ${host:-factory-core.tailnet.invalid}" \
    -H "X-Forwarded-Proto: https" http://127.0.0.1:8080/api/v1/server_settings || true)
  g=$(curl -s -o /dev/null -w '%{http_code}' "$GITEA/api/healthz" || true)
  log "health: Zulip server_settings HTTP $z, Gitea healthz HTTP $g"
  docker ps --format '  {{.Names}}: {{.Status}}' | sort
  for u in factory-omp-broker factory-supervisor factory-backup.timer; do
    printf '  %s: %s\n' "$u" "$(systemctl is-active "$u" || true)"
  done
}

# -- restore --------------------------------------------------------------------------------------------------------
cmd_restore() {
  local from="" pw="" snap="" takeover=0 gone=0
  while [ $# -gt 0 ]; do
    case "$1" in
      --from) from=${2:?}; shift 2 ;;
      --password-file) pw=${2:?}; shift 2 ;;
      --snapshot) snap=${2:?}; shift 2 ;;
      --take-over) takeover=1; shift ;;
      --old-is-gone) gone=1; shift ;;
      *) die "restore: unknown argument $1" ;;
    esac
  done
  [ -n "$from" ] && [ -n "$pw" ] || die "usage: restore --from <repository> --password-file <file> [--snapshot <id>] [--take-over [--old-is-gone]]"
  [ -r "$pw" ] || die "cannot read $pw"
  pw=$(readlink -f "$pw")
  case "$(role)" in
    none) ! docker volume inspect factory-gitea_gitea-data >/dev/null 2>&1 ||
            die "this server already runs a factory that this script did not set up; restore only onto a new server" ;;
    fresh|rehearsal) ;;
    *) die "this server is $(role); a restore would overwrite it" ;;
  esac

  packages
  log "backup repository: $from"
  install -d -m 700 /srv/factory-backup
  case "$from" in
    sftp:*|*:/*) rsync -a --delete -e "ssh -o BatchMode=yes" "${from#sftp:}/" "$BACKUP_REPO/" ;;
    *) [ -f "$from/config" ] || die "not a restic repository: $from"
       [ "$(readlink -f "$from")" = "$BACKUP_REPO" ] || rsync -a --delete "$from/" "$BACKUP_REPO/" ;;
  esac
  export RESTIC_REPOSITORY=$BACKUP_REPO RESTIC_PASSWORD_FILE=$pw RESTIC_CACHE_DIR=/var/cache/restic
  restic check -q >/dev/null 2>&1 || die "restic check failed (wrong password file or damaged copy)"
  local sj
  sj=$(restic snapshots --json)
  if [ -n "$snap" ]; then
    sj=$(jq -c --arg s "$snap" '[.[] | select(.short_id == $s or .id == $s)] | last' <<<"$sj")
  else
    sj=$(jq -c 'sort_by(.time) | last' <<<"$sj")
  fi
  [ "$sj" != null ] || die "snapshot not found"
  snap=$(jq -r .short_id <<<"$sj")
  local stime final
  stime=$(jq -r .time <<<"$sj")
  final=$(jq -r '(.tags // []) | index("final") != null' <<<"$sj")
  log "snapshot $snap of $stime (tags: $(jq -r '(.tags // []) | join(",")' <<<"$sj"))"
  if [ "$takeover" = 1 ] && [ "$final" != true ] && [ "$gone" != 1 ]; then
    die "take-over refused: snapshot $snap is not the old server's final backup. Stop the old server first
(sudo bash $REPO/infra/factory.sh stop there), copy the backup again, then repeat. If the old server is lost for
good, add --old-is-gone. Nothing on this server was changed except its copy of the backup."
  fi

  # Held from here on: nothing of a fresh install or an earlier rehearsal may run with restored data.
  hold "$HOLD" "restore in progress"
  systemctl stop factory-supervisor factory-omp-broker 2>/dev/null || true
  rm -f /etc/factory/omp-broker.token
  firewall

  log "clearing the services of the previous install on this server"
  [ -f "$F/zulip/compose.yaml" ] && zc down -v --remove-orphans >/dev/null 2>&1 || true
  [ -f "$F/gitea/compose.yaml" ] && gc down -v --remove-orphans >/dev/null 2>&1 || true
  docker compose ls --all --format json 2>/dev/null | jq -r '.[].Name | select(startswith("factory-lab-"))' |
    while read -r p; do docker compose -p "$p" down -v >/dev/null 2>&1 || true; done

  log "restic restore $snap -> $WORK"
  rm -rf "$WORK"
  local ex=()
  [ "$takeover" = 1 ] || ex=(--exclude /var/lib/omp-broker)
  restic restore "$snap" --target "$WORK" "${ex[@]}" >/dev/null
  if [ "$takeover" != 1 ]; then
    log "rehearsal: model login and push registration removed from the restored copy"
    strip_credentials
  fi

  log "/srv/factory: secrets, compose settings, workspaces"
  install -d -m 700 -o root -g root "$S"
  rsync -a --delete "$WORK$F/secrets/" "$S/"
  rsync -a --exclude /secrets "$WORK$F/" "$F/"
  files "$SRC"
  local push=False
  if [ "$takeover" = 1 ] && grep -q '^zulip_org_id' "$WORK$VOL/factory-zulip_zulip/_data/zulip-secrets.conf"; then
    push=True
  fi
  setenv "$F/zulip/.env" ZULIP_PUSH_NOTIFICATIONS $push

  log "Gitea: database from the dump, data volume from the snapshot"
  gc up -d --quiet-pull --wait gitea-db >/dev/null
  gc exec -T gitea-db psql -q -U gitea -d gitea -v ON_ERROR_STOP=1 \
    < "$WORK/srv/factory-backup/staging/gitea-db.sql" >/dev/null
  gc run --rm --no-deps -v "$WORK$VOL/factory-gitea_gitea-data/_data:/src:ro" --entrypoint sh gitea \
    -c 'cp -a /src/. /data/' >/dev/null
  gc up -d --quiet-pull >/dev/null
  wait_gitea
  sync_copy

  log "Zulip: data volume from the snapshot, database from its dump"
  zc run --rm --no-deps -v "$WORK$VOL/factory-zulip_zulip/_data:/src:ro" --entrypoint sh zulip \
    -c 'cp -a /src/. /data/' >/dev/null
  local dump
  dump=$(cd "$WORK$VOL/factory-zulip_zulip/_data/backups" && ls -1t backup-* 2>/dev/null | head -1)
  [ -n "$dump" ] || die "no Zulip dump in the snapshot"
  zc run --rm zulip app:restore "$dump" 2>&1 | tail -1 | sed 's/^/  /'
  zulip_up

  set_role "$([ "$takeover" = 1 ] && echo live || echo rehearsal)"
  if [ "$takeover" = 1 ]; then
    hold "$HOLD" "take-over in progress"
  else
    hold "$HOLD" "rehearsal restore of snapshot $snap ($stime): the old server is live; take over with factory.sh restore ... --take-over after stopping it"
  fi
  rm -f "$HOLD-supervisor"
  deploy

  log "agent users: OMP homes, workspaces; supervisor state"
  local d u
  for d in "$WORK"/home/agent-*; do
    [ -d "$d/.omp" ] || continue
    u=$(basename "$d")
    if ! id "$u" >/dev/null 2>&1; then echo "  skipped $u: no such product any more"; continue; fi
    rsync -a --delete "$d/.omp/" "/home/$u/.omp/"
    chown -R "$u:$u" "/home/$u/.omp"
  done
  if [ -d "$WORK/home/factory/.omp" ] && id factory >/dev/null 2>&1; then
    rsync -a --delete "$WORK/home/factory/.omp/" /home/factory/.omp/
    chown -R factory:factory /home/factory/.omp
  fi
  for d in "$F"/workspaces/agent-*; do
    [ -d "$d" ] || continue
    u=$(basename "$d")
    if id "$u" >/dev/null 2>&1; then chown -R "$u:$u" "$d"; chmod 700 "$d"; fi
  done
  if [ -d "$WORK/var/lib/factory-supervisor" ]; then
    install -d -m 700 /var/lib/factory-supervisor
    rsync -a --delete "$WORK/var/lib/factory-supervisor/" /var/lib/factory-supervisor/
    chown -R factory-supervisor:factory-supervisor /var/lib/factory-supervisor
    chmod 700 /var/lib/factory-supervisor
  fi
  /usr/local/sbin/factory-product-admin sync >/dev/null   # the playbooks again, over the restored homes

  if [ "$takeover" = 1 ]; then
    rsync -a --delete "$WORK/var/lib/omp-broker/.omp/" /var/lib/omp-broker/.omp/
    chown -R omp-broker:omp-broker /var/lib/omp-broker
    chmod 700 /var/lib/omp-broker /var/lib/omp-broker/.omp
    rm -rf "$WORK"
    backup_timer
    local name
    name=$(sed -n 's/^ZULIP_EXTERNAL_HOST=//p' "$F/zulip/.env" | tail -1 | cut -d. -f1)
    rm -f "$HOLD"
    cmd_tailnet --hostname "${name:-factory-core}"
    report_health
    log "take-over done: this server is the factory now (snapshot $snap of $stime)"
  else
    systemctl disable --now factory-backup.timer >/dev/null 2>&1 || true
    rm -rf "$WORK"
    report_health
    cat <<EOF

Rehearsal restore done (snapshot $snap of $stime). This server holds a full copy of the factory but cannot act as
it: no model login, broker and supervisor held, no push registration, not on the tailnet, no nightly backup.
To move for real: on the old server  sudo bash $REPO/infra/factory.sh stop ; copy its backup here again; then
  sudo bash $REPO/infra/factory.sh restore --from <repository> --password-file <file> --take-over
EOF
  fi
}

strip_credentials() { # rehearsal: no copy of the model login and no Zulip push identity may survive in $WORK
  local db n total=0
  while IFS= read -r -d '' db; do
    n=$(python3 - "$db" <<'EOF'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
if c.execute("select 1 from sqlite_master where type='table' and name='auth_credentials'").fetchone():
    n = c.execute("delete from auth_credentials").rowcount
    c.commit()
    c.execute("vacuum")
    c.execute("pragma wal_checkpoint(truncate)")
    print(n)
else:
    print(0)
c.close()
EOF
)
    total=$((total + n))
  done < <(find "$WORK/home" "$WORK/var/lib" -name agent.db -path '*/.omp/*' -print0 2>/dev/null)
  echo "  model logins deleted from restored OMP homes: $total; broker store not restored"
  local zs="$WORK$VOL/factory-zulip_zulip/_data/zulip-secrets.conf"
  if [ -f "$zs" ]; then
    sed -i '/^zulip_org_id/d; /^zulip_org_key/d' "$zs"
    echo "  Zulip push registration (zulip_org_id/zulip_org_key) removed from the copy"
  fi
}

# -- stop (old server) ----------------------------------------------------------------------------------------------
cmd_stop() {
  [ "$(role)" != rehearsal ] || die "this is a rehearsal copy, not the live factory"
  log "supervisor and broker: stop (agents stop with them)"
  systemctl stop factory-supervisor factory-omp-broker
  hold "$HOLD" "stopped for a move on $(date -u +%FT%TZ): the factory runs on the new server now"
  log "final backup (tag final)"
  FACTORY_BACKUP_TAG=final RESTIC_CACHE_DIR=/var/cache/restic "$F/backup/backup.sh" 2>&1 | sed 's/^/  /'
  log "Zulip, Gitea, runners: stop"
  docker compose ls --format json | jq -r '.[].Name | select(startswith("factory-lab-"))' |
    while read -r p; do docker compose -p "$p" stop >/dev/null; done
  zc stop >/dev/null
  gc stop >/dev/null
  systemctl disable --now factory-backup.timer >/dev/null 2>&1 || true
  set_role retired
  local sid
  sid=$(RESTIC_REPOSITORY=$BACKUP_REPO RESTIC_PASSWORD_FILE=$S/restic_password restic snapshots --json --tag final |
    jq -r 'sort_by(.time) | last | .short_id')
  cat <<EOF

Stopped. Final backup: snapshot $sid in $BACKUP_REPO (tag final). Next:
  1. Copy the backup to the new server (or let it fetch it: --from <this server>:$BACKUP_REPO).
  2. Tailscale admin console (phone: Machines): remove this server's old entry, so the new one gets its name.
  3. On the new server: sudo bash $REPO/infra/factory.sh restore --from <repository> --password-file <file> --take-over
Logging this server out of Tailscale now (a session over the tailnet ends here).
EOF
  tailscale logout 2>/dev/null || true
}

# -- tailnet and owner steps ------------------------------------------------------------------------------------------
cmd_tailnet() {
  local name=""
  [ "${1:-}" = --hostname ] && name=${2:?}
  [ ! -e "$HOLD" ] || die "held ($(head -1 "$HOLD")): this server must not join the tailnet"
  if [ -z "$name" ]; then
    name=$(sed -n 's/^ZULIP_EXTERNAL_HOST=//p' "$F/zulip/.env" | tail -1 | cut -d. -f1)
    case "$name" in ""|*invalid*) name=factory-core ;; esac
  fi
  log "Tailscale: joining as $name. Open the link below (or scan the QR code) on your phone and approve."
  tailscale up --hostname="$name" --qr
  local dns
  dns=$(tailscale status --json | jq -r '.Self.DNSName' | sed 's/\.$//')
  case "$dns" in
    "$name".*) ;;
    *) echo "WARNING: the tailnet named this server '$dns', not '$name.*': another device still has the name."
       echo "Remove the old device in the Tailscale admin console, rename this one to $name, then run: factory.sh tailnet" ;;
  esac
  bash "$F/tailnet-serve.sh" | sed 's/^/  /'
  rm -f "$HOLD-supervisor"
  bash "$REPO/infra/supervisor/install.sh" 2>&1 | tail -3 | sed 's/^/  /'
}

cmd_push_register() {
  [ ! -e "$HOLD" ] || die "held ($(head -1 "$HOLD")): no push registration from this server"
  setenv "$F/zulip/.env" ZULIP_PUSH_NOTIFICATIONS True
  zc up -d --wait zulip >/dev/null
  zc exec -u zulip zulip /home/zulip/deployments/current/manage.py register_server --agree-to-terms-of-service
}

main() {
  [ "$(id -u)" = 0 ] || die "run as root (sudo bash $0 ...)"
  local cmd=${1:-}
  shift || true
  case "$cmd" in
    install) cmd_install "$@" ;;
    restore) cmd_restore "$@" ;;
    stop) cmd_stop ;;
    tailnet) cmd_tailnet "$@" ;;
    owner-link) bash "$F/zulip/owner-link.sh" "$OWNER_EMAIL" ;;
    push-register) cmd_push_register ;;
    import-login) bash "$REPO/infra/supervisor/import-login.sh" "${1:?file}"; bash "$REPO/infra/supervisor/install.sh" | tail -2 ;;
    *) sed -n '2,40p' "$0"; exit 2 ;;
  esac
  log "done"
}
main "$@"
