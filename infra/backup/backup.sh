#!/bin/bash
# Nightly factory backup (systemd timer factory-backup.timer) into a local restic repository.
# Consistent database dumps first, then one restic snapshot of: Gitea dump + data volume, Zulip data volume (with
# its fresh pg_dump), the OMP homes (factory user if any, auth broker with the model login, agent users: sessions,
# memory), the supervisor's state, and /srv/factory (compose files, secrets, task workspaces, the release signer's
# keys, token and products; the repository is encrypted). Off-site copy: infra/offsite/pull-backup.py. Run as
# root.
# FACTORY_BACKUP_TAG=final (infra/factory.sh stop): the last snapshot of a server that is handing over; a restore
# with --take-over looks for it.
set -euo pipefail
export RESTIC_REPOSITORY=${RESTIC_REPOSITORY:-/srv/factory-backup/restic}
export RESTIC_PASSWORD_FILE=/srv/factory/secrets/restic_password
STAGE=/srv/factory-backup/staging
VOL=/var/lib/docker/volumes
TAGS=(--tag nightly)
if [ -n "${FACTORY_BACKUP_TAG:-}" ]; then TAGS+=(--tag "$FACTORY_BACKUP_TAG"); fi
# OMP homes of the factory user, the auth broker and the agent users (infra/supervisor), when present
EXTRA=()
for d in /home/factory/.omp /var/lib/omp-broker/.omp /home/agent-*/.omp /var/lib/factory-supervisor; do
  if [ -d "$d" ]; then EXTRA+=("$d"); fi
done
log() { echo "$(date -u +%FT%TZ) $*"; }

mkdir -p "$STAGE"
restic cat config >/dev/null 2>&1 || restic init

log "gitea: pg_dump"
(cd /srv/factory/gitea && docker compose exec -T gitea-db pg_dump -U gitea -d gitea --clean --if-exists) \
  > "$STAGE/gitea-db.sql.tmp"
mv "$STAGE/gitea-db.sql.tmp" "$STAGE/gitea-db.sql"

log "zulip: app:backup (pg_dump into the zulip volume)"
(cd /srv/factory/zulip && docker compose exec -T zulip /sbin/entrypoint.sh app:backup)
# Keep only the newest three dumps inside the volume; restic keeps the history.
ls -1t "$VOL"/factory-zulip_zulip/_data/backups/backup-*.sql* 2>/dev/null | tail -n +4 | xargs -r rm -f

log "restic backup"
restic backup "${TAGS[@]}" --host factory-core \
  "$STAGE" \
  "$VOL/factory-gitea_gitea-data/_data" \
  "$VOL/factory-zulip_zulip/_data" \
  "${EXTRA[@]}" \
  /srv/factory \
  --exclude "**/.omp/logs" --exclude "**/.omp/**/cache"
restic forget --tag nightly --host factory-core --keep-daily 14 --keep-weekly 8 --keep-monthly 6 --prune
restic check
log "done"
