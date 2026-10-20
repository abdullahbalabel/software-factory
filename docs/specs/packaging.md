# Spec: packaging and deployment

## Core as Docker Compose

| Service | Image (indicative) | Volume |
|---|---|---|
| zulip | docker-zulip `ghcr.io/zulip/zulip-server:12.3-0` (project `factory-zulip`, `/srv/factory/zulip`) | zulip (`/data`: uploads, secrets, dumps) |
| zulip-db / redis / rabbitmq / memcached | as required by docker-zulip | per service |
| gitea | `gitea/gitea:28.1.0` (project `factory-gitea`, `/srv/factory/gitea`) | gitea-data |
| gitea-db | `postgres:17.11-alpine` | gitea-db |
| gitea-runner | `gitea/act_runner:0.6.1` (Docker socket for the runner, not for jobs); lab runners per product (`factory-lab-<org>`) | runner-data |
| supervisor | **as built (F2): systemd services on the host, not a container** (`factory-supervisor`, `factory-omp-broker`; agents run as separate Unix users, see [supervisor.md](supervisor.md#deployment)); code from this repository, OMP in `/opt/factory-omp` | OMP homes of the broker and agent users, `/var/lib/factory-supervisor` (in the nightly backup) |
| tailscale | **as built: `tailscaled` on the host** (package from pkgs.tailscale.com); `tailscale serve` publishes Zulip and Gitea on the tailnet only | `/var/lib/tailscale` (not in the backup: a new server joins anew) |
| backup | restic 0.16, nightly systemd timer `factory-backup` ([backup.sh](../../infra/backup/backup.sh)); off-site copy pulled to another computer ([pull-backup.py](../../infra/offsite/pull-backup.py)) | `/srv/factory-backup/restic` |

Task workspaces live in `/srv/factory/workspaces/<agent user>/` on the server (in the nightly backup).

## Where it runs

- Any **Ubuntu 24.04** server with 4 vCPU, 16 GB RAM and 150 GB disk: a VM on a KVM host
  ([infra/host/mk-factory-vm.sh](../../infra/host/mk-factory-vm.sh)) or a plain cloud server.
  No domain and no open port besides SSH: the owner reaches it only over Tailscale.

## As built (F6, 18 Oct 2026): install and restore with one command

[infra/factory.sh](../../infra/factory.sh), run as root from a git checkout of this repository. Owner guide:
[README, "Move or reinstall the factory"](../../README.md#move-or-reinstall-the-factory).

| Command | What it does |
|---|---|
| `install` | Docker (download.docker.com), Tailscale (installed, not joined), restic; the server firewall ([infra/core/factory-host.nft](../../infra/core/factory-host.nft), own nftables table `inet factory_host`, unit `factory-firewall`: inbound only SSH, Tailscale's UDP port, ICMP, DHCP; every service is on the loopback); `/srv/factory` with the Compose files and generated secrets; Zulip (organisation, owner account without a password, supervisor bot, streams: [bootstrap-realm.py](../../infra/core/zulip/bootstrap-realm.py)); Gitea + PostgreSQL (accounts `owner`, must change its password, and `factory-ops` with its token) and the runner (no Docker socket in jobs); [provision.py](../../infra/supervisor/provision.py) (factory and product projects); this checkout pushed as `factory/software-factory`; then [update.sh](../../infra/supervisor/update.sh): the deployed checkout, lab runners, OMP (bun 1.4.3 from GitHub when no local copy), broker, agent users, helpers, sudo rules, `/proc` hidepid; nightly backup timer. Idempotent. The supervisor waits for `tailnet`. Ends with the owner's steps, prints no secret. |
| `restore --from <repo> --password-file <file> [--snapshot <id>] [--take-over [--old-is-gone]]` | The same, then the data of a restic snapshot ([backup.sh](../../infra/backup/backup.sh)): `/srv/factory` (secrets, `.env`, workspaces), Gitea (dump into a new database, data volume), Zulip (data volume, `app:restore` of its dump), the Gitea copy of this repository (fast-forwarded to the checkout when that is newer) and with it the product registry, runners, agent users with their OMP homes and workspaces, the supervisor's state. `<repo>`: a local restic repository (a copy of the off-site backup) or `[sftp:][user@]host:/path` over SSH (rsync); it becomes the server's backup repository. Refuses a server that runs a factory it did not set up. |
| `stop` | On the old server of a move: stops supervisor and broker, final backup tagged `final`, stops Zulip, Gitea, runners, the backup timer, Tailscale (logout). |
| `tailnet [--hostname]` | `tailscale up --qr` (the owner approves on his phone), [tailnet-serve.sh](../../infra/core/tailnet-serve.sh) (Zulip 443, Gitea 8443/2222 on the tailnet; Zulip `EXTERNAL_HOST`, Gitea `ROOT_URL` and `/etc/factory-supervisor/site.yml`, which overrides `zulip.site`/`gitea.public` of `supervisor/config.yml`), then starts the supervisor. |
| `owner-link`, `push-register`, `import-login <file>` | The owner's one-time Zulip password link; Zulip push registration after his consent (sets `ZULIP_PUSH_NOTIFICATIONS=True` in Zulip's `.env`); his model login export into the broker (the file is deleted). |

**Rehearsal and take-over.** A restore without `--take-over` is a rehearsal and is safe while the old server runs:
the broker's store (the model login) is not restored and any login left in the agents' OMP homes is deleted; the
hold file `/var/lib/factory/hold` keeps broker and supervisor from starting (conditions in their units, so also after
a reboot; `install.sh`, `tailnet`, `push-register` and `import-login` refuse); Zulip's push identity
(`zulip_org_id`/`zulip_org_key`) is removed from the restored volume and push, usage statistics and security alerts
are off (`ZULIP_PUSH_NOTIFICATIONS=False`), so the copy never contacts Zulip's push service; no tailnet, no nightly
backup. `--take-over` is accepted only with the old server's `final` snapshot (`--old-is-gone` when it is lost): it
also restores the login and the push identity (push on if the old server was registered), joins the tailnet under
the old name (the owner removes the old device in Tailscale first), publishes Zulip and Gitea, enables the backup
and starts broker and supervisor. A fresh install holds only the supervisor (`hold-supervisor`) until `tailnet`.
Roles in `/var/lib/factory/role`: `fresh`, `rehearsal`, `live` (after take-over; a restore then refuses), `retired`
(after `stop`).

**Compare two servers:** [infra/backup/inventory.py](../../infra/backup/inventory.py) prints Gitea repositories
(head, issues, pull requests), Zulip (organisation, streams, owner, message count up to a time), supervisor state,
agent users and stored logins, service states, as JSON without secrets.
