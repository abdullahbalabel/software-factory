#!/bin/bash
# Restore test: restore the latest restic snapshot into /srv/restore-test and start FRESH copies of Gitea and Zulip
# from it alone (own Compose projects, own volumes, secrets taken from the restored files, ports 13000/18080 on
# loopback), check the data, then remove the copies. The live stack is not touched. Run as root on the factory VM.
set -euo pipefail
export RESTIC_REPOSITORY=${RESTIC_REPOSITORY:-/srv/factory-backup/restic}
export RESTIC_PASSWORD_FILE=/srv/factory/secrets/restic_password
export RESTIC_CACHE_DIR=/var/cache/restic
R=/srv/restore-test
VOLS=var/lib/docker/volumes
KEEP=${KEEP:-0}
log() { echo "$(date -u +%FT%TZ) $*"; }

rm -rf "$R"; mkdir -p "$R"
SNAP=$(restic snapshots --json --latest 1 | jq -r '.[-1].short_id')
log "restore snapshot $SNAP -> $R"
restic restore "$SNAP" --target "$R" >/dev/null
S=$R/srv/factory/secrets

# ---- Gitea -------------------------------------------------------------------------------------------------------
G=$R/srv/factory/gitea
cat >"$G/restore.yaml" <<EOF
secrets:
  gitea_db_password: {file: $S/gitea_db_password}
  gitea_runner_token: {file: $S/gitea_runner_token}
services:
  gitea:
    ports: !override
      - "127.0.0.1:13000:3000"
  runner:
    profiles: [never]
EOF
GC="docker compose -p factory-gitea-restore -f $G/compose.yaml -f $G/restore.yaml"
log "gitea: fresh database from the dump"
$GC up -d --wait gitea-db
$GC exec -T gitea-db psql -q -U gitea -d gitea -v ON_ERROR_STOP=1 < "$R/srv/factory-backup/staging/gitea-db.sql" >/dev/null
log "gitea: fresh data volume from the snapshot"
$GC run --rm --no-deps -v "$R/$VOLS/factory-gitea_gitea-data/_data:/src:ro" --entrypoint sh gitea -c 'cp -a /src/. /data/'
$GC up -d gitea
for i in $(seq 1 60); do curl -sf http://127.0.0.1:13000/api/healthz >/dev/null && break; sleep 2; done
A=(-H "Authorization: token $(cat "$S/gitea_ops_token")")
echo "gitea-restore: repo:      $(curl -sf "${A[@]}" http://127.0.0.1:13000/api/v1/repos/owner/ci-smoke | jq -c '{full_name,private,default_branch}')"
echo "gitea-restore: workflow:  $(curl -sf "${A[@]}" http://127.0.0.1:13000/api/v1/repos/owner/ci-smoke/contents/.gitea/workflows/smoke.yaml | jq -r '.name')"
echo "gitea-restore: run:       $(curl -sf "${A[@]}" http://127.0.0.1:13000/api/v1/repos/owner/ci-smoke/actions/tasks | jq -c '[.workflow_runs[]|{run_number,status}]')"
echo "gitea-restore: users:     $(curl -sf "${A[@]}" http://127.0.0.1:13000/api/v1/admin/users | jq -c '[.[].login]')"
echo "gitea-restore: git clone: $(git clone -q "http://factory-ops:$(cat "$S/gitea_ops_token")@127.0.0.1:13000/owner/ci-smoke.git" "$R/clone" && git -C "$R/clone" log --oneline | wc -l) commits"

# ---- Zulip -------------------------------------------------------------------------------------------------------
Z=$R/srv/factory/zulip
cat >"$Z/restore.yaml" <<EOF
secrets:
  zulip__postgres_password: {file: $S/zulip__postgres_password}
  zulip__memcached_password: {file: $S/zulip__memcached_password}
  zulip__rabbitmq_password: {file: $S/zulip__rabbitmq_password}
  zulip__redis_password: {file: $S/zulip__redis_password}
  zulip__secret_key: {file: $S/zulip__secret_key}
  zulip__email_password: {file: $S/zulip__email_password}
services:
  zulip:
    ports: !override
      - "127.0.0.1:18080:80"
EOF
ZC="docker compose -p factory-zulip-restore -f $Z/compose.yaml -f $Z/compose.override.yaml -f $Z/restore.yaml"
log "zulip: fresh data volume from the snapshot"
$ZC run --rm --no-deps -v "$R/$VOLS/factory-zulip_zulip/_data:/src:ro" --entrypoint sh zulip -c 'cp -a /src/. /data/'
DUMP=$(ls -1t "$R/$VOLS/factory-zulip_zulip/_data/backups/" | grep '^backup-' | head -1)
log "zulip: fresh database from $DUMP"
$ZC run --rm zulip app:restore "$DUMP" 2>&1 | tail -2
$ZC up -d --wait zulip
HOST=$(sed -n 's/^ZULIP_EXTERNAL_HOST=//p' "$Z/.env" 2>/dev/null || true); HOST=${HOST:-factory-core.tailnet.invalid}
H=(-H "Host: $HOST" -H "X-Forwarded-Proto: https")
BOT="supervisor-bot@factory-core.tailnet.invalid:$(cat "$S/zulip_supervisor_bot_api_key")"
echo "zulip-restore: server:    $(curl -sf "${H[@]}" http://127.0.0.1:18080/api/v1/server_settings | jq -c '{zulip_version,realm_name}')"
echo "zulip-restore: streams:   $(curl -sf "${H[@]}" -u "$BOT" http://127.0.0.1:18080/api/v1/users/me/subscriptions | jq -c '[.subscriptions[].name]')"
echo "zulip-restore: messages:  $(curl -sf "${H[@]}" -u "$BOT" -G http://127.0.0.1:18080/api/v1/messages --data-urlencode 'narrow=[{"operator":"channel","operand":"sandbox"}]' -d anchor=newest -d num_before=5 -d num_after=0 | jq -c '[.messages[]|{id,subject,content:(.content[0:60])}]')"

# ---- OMP home ----------------------------------------------------------------------------------------------------
echo "omp-home-restore: $(find "$R/home/factory/.omp" -type f | wc -l) files (live: $(find /home/factory/.omp -type f -not -path '*/logs/*' | wc -l))"

if [ "$KEEP" != 1 ]; then
  log "cleanup"
  $ZC down -v >/dev/null 2>&1; $GC down -v >/dev/null 2>&1; rm -rf "$R"
fi
log "restore test done"
