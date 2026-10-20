#!/bin/bash
# Create the factory's generated secrets once, in /srv/factory/secrets (dir 0700 root; files 0644 so that container
# entrypoints running as non-root can read their bind-mounted copy). Existing files are never overwritten.
# No trailing newline (docker-zulip requires that). Values are never printed.
set -euo pipefail
D=/srv/factory/secrets
sudo install -d -m 0700 -o root -g root "$D"
gen() { # name length
  local f="$D/$1"
  if sudo test -e "$f"; then echo "kept: $1"; return; fi
  sudo sh -c "umask 022; head -c 256 /dev/urandom | tr -dc 'A-Za-z0-9' | head -c $2 > '$f'"
  echo "created: $1"
}
gen gitea_db_password 40
gen gitea_owner_password 28
gen zulip__postgres_password 40
gen zulip__memcached_password 40
gen zulip__rabbitmq_password 40
gen zulip__redis_password 40
gen zulip__secret_key 64
gen restic_password 48
# Filled later: runner registration token (from Gitea), outgoing email password (no SMTP yet).
for f in gitea_runner_token zulip__email_password; do
  sudo test -e "$D/$f" || { sudo sh -c "umask 022; : > '$D/$f'"; echo "created empty: $f"; }
done
