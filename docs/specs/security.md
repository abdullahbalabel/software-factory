# Spec: security

- **Network:** every service listens only on the tailnet; no public ports on the factory server or the hands.
- **Accounts:** the owner's Zulip and Gitea accounts with two-factor sign-in; bots have their own accounts with the
  least rights; bots cannot approve.
- **Secrets:** stored as container secrets or in OMP's credential store, never in repositories or messages; per
  product. Release signing keys (D22): Android keys only in the factory signer's folder (`/srv/factory/signer`, user
  `factory-signer`), never in Gitea, a repository, an agent's workspace or a model's context; CI builds unsigned and
  the signer signs successful `main` builds ([infra/signer](../../infra/signer/factory-signer)). Windows signing is not
  built yet.
- **Agents:** a task's workspace contains only its product; agents push only to their task branches; merging to a
  release branch and publishing need the release approval.
- **Scans:** secret scanning before every push to GitHub; dependency and image scans in the gates.
- **Backups:** nightly, encrypted, off-site; restore tested monthly.
- **Audit:** the decision log and Gitea history record who approved what and when.

## As built (F2): agent isolation

Agent sessions run headless with tools auto-approved (`--approval-mode yolo`), so the operating-system account they
run as is the control. Set up by [infra/supervisor/install.sh](../../infra/supervisor/install.sh) and
[provision.py](../../infra/supervisor/provision.py); checked on the VM on 17 Oct 2026 (UTC).

| Account | Runs | Can | Cannot |
|---|---|---|---|
| `factory-supervisor` (system user) | the supervisor service | read its secrets, handed over by systemd only (`LoadCredential` from root-only `/srv/factory/secrets/supervisor/`: Zulip bot key, Gitea admin token, agents' Gitea tokens); run `/usr/local/sbin/factory-agent-exec` as the agent users, and `/usr/local/sbin/factory-product-admin` as root (`/product new|remove`; validates every argument; logged by sudo); see all processes (group `procview`) | log in; use docker; any other command as root |
| `agent-<product>` (e.g. `agent-sandbox`) | that product's task sessions | its workspaces `/srv/factory/workspaces/agent-<product>/` (0700); its own OMP home (`~/.omp`, profile `<product>`, with the factory's playbook skills copied in as this user); its product's Gitea account (`sandbox-agent`: write on `sandbox/playground` only, token scope `write:repository`; the default branch is protected, so pushes go to task branches) | sudo, docker (not in the groups; checked by the installer); merge a pull request into the default branch (merge whitelist: only the supervisor's admin account `factory-ops`; the agent's token gets 405); read `/srv/factory/secrets` (root 0700), the supervisor's state, the broker's store, other agents' homes (0700) or the factory user's home (0750) |
| `agent-chat` | chat sessions | a read-only clone of `factory/software-factory`; Gitea read-only (`factory-reader`: read on the factory repositories and every product repository; scopes `read:repository,read:issue,read:organization,read:user`) | write to any repository; the same "cannot" as above |
| `omp-broker` (system user) | `omp auth-broker serve` on 127.0.0.1:8765 | hold and refresh the owner's model login (home `/var/lib/omp-broker`, 0700) | — |

- **Model login:** copied once from the factory user's OMP into the broker (`import-login.sh`, through a pipe). Agent
  users reach it as broker clients (`OMP_AUTH_BROKER_URL`, bearer token in `/etc/factory/omp-broker.token`, group
  `factory-agents`, 0640): they receive short-lived access tokens, never the refresh token, and the login is
  refreshed in one place only. The factory user's own OMP copy must not be used for model work any more (two holders
  refreshing one login can invalidate each other).
- **OMP itself** is one root-owned install in `/opt/factory-omp`; agents cannot change it.
- **Headless start:** every `omp` the supervisor starts has the RPC pipe or `/dev/null` as stdin, never a terminal.
- **Wrapper:** `factory-agent-exec` accepts eight actions only (`omp`, `set-token`, `workspace` with a Gitea URL on
  127.0.0.1, `rm-workspace`, `stop-agents`, `put-prompt`, `rm-prompt`, `put-file` into a workspace's `attachments/`
  with a size cap) and refuses to run as anything but an `agent-*` user.
- **Prompts off the command line (S1, 18 Oct 2026):** the agent system prompt is written by the agent user to
  `~/.factory/prompts/<id>.md` (0600, home 0700) and passed as `--append-system-prompt <path>`; OMP reads it at
  start and the supervisor removes it as soon as the session answers. Task and chat text always went over the RPC
  pipe. sudo no longer logs allowed `factory-agent-exec` runs (`Defaults!/usr/local/sbin/factory-agent-exec
  !log_allowed`; before, every prompt reached journald through sudo's command line); `log_input` stays off;
  `factory-product-admin` runs are logged.
- **/proc (S1):** mounted `hidepid=invisible,gid=procview` (in `/etc/fstab`): each agent user sees only its own
  processes, so one agent cannot read another's command line or environment; the supervisor (group `procview`)
  sees all. Checked: `agent-sandbox` lists none of `agent-chat`'s processes and cannot read their
  `/proc/<pid>/cmdline`, and the reverse.
- **Server firewall and restores (F6):** `infra/factory.sh` installs an
  inbound firewall on the server itself (own nftables table `inet factory_host`: only SSH, Tailscale's UDP port, ICMP
  and DHCP from outside the loopback, the tailnet and the Docker networks). A restore that is not a take-over (a
  rehearsal while the old server runs) gets no model login (the broker's store is not restored, any login in the
  agents' OMP homes is deleted), no Zulip push identity (removed; push, usage statistics and security alerts off),
  no tailnet and no supervisor: the hold file `/var/lib/factory/hold` is a start condition of the broker and
  supervisor units, so they stay down also after a reboot or a deploy. A take-over needs the old server's final
  backup (`factory.sh stop`), so two servers never hold and refresh the same login.
- **Products from chat:** `/product new` creates a new agent user and Gitea account with the same limits as
  `agent-sandbox`/`sandbox-agent` (the root helper rewrites the sudo rule to list exactly the registered agent
  users); `/product remove` cannot remove the default product.
- **Owner only:** the supervisor acts only on messages and reactions of organisation owners (role 100); the bot and
  other members are ignored, so nobody else can approve, change settings or start work.
- **Not from chat:** agent users, tokens, approval mode and sandboxing are not settings the owner (or an agent posing
  as text in a topic) can change through Zulip.
- **Known limits:** agents share the VM's network (they can reach the internet, Gitea and Zulip's web front on
  loopback, without credentials beyond their own). Restic snapshots follow the retention policy (14 daily, 8 weekly,
  6 monthly).
- **Gates (F3):** workflow files are part of each product repository, so an agent can change what its own gates run
  (the playbooks and the fix prompt forbid weakening a check; the owner sees the diff in the pull request). Jobs run
  in unprivileged containers on the VM's Docker (no volumes, `privileged: false`), on the `factory-gitea_default`
  network with the job's own repository-scoped token, and can reach the internet (for `actions/checkout`,
  packages). **Corrected 18 Oct 2026 (F4):** act_runner's default (`container.docker_host` empty) also mounted
  `/var/run/docker.sock` into every job container, which gave any workflow root on the VM; both runners now set
  `docker_host: "-"` (socket not mounted; job containers checked with `docker inspect`: `Binds []`).

## Hands

As built F4 (18 Oct 2026); design in [architecture.md](../architecture.md#hands-optional-per-product).

- **No agent on a hand (D8).** A hand is only a Gitea Actions runner. It connects **out** to Gitea
  (`https://<server>.<tailnet>.ts.net:8443`, tailnet only) and listens on nothing; the factory never logs in to
  it. What it runs is what the product's workflow says, so a hand trusts the product's repository as much as the
  factory's Linux runner does (the owner sees workflow changes in the pull request; merging a change to the gates
  needs his explicit `merge with gate change: <reason>`, below).
- **Scoped to one product:** a hand's runner is registered to the product's Gitea organisation, so workflows of other
  products (and of `factory/*`) cannot reach it, even with the right `runs-on` label. A product declares its hands in
  `product.yml`; the supervisor tells agents only about those and names jobs that wait for an undeclared or offline
  hand.
- **Lab hand (factory VM):** runner container `lab-<org>` (act_runner 0.6.1) with the Docker socket like
  `factory-core-1`; its jobs run in `factory-lab:1` containers, unprivileged, no volumes, no Docker socket, with only
  `/dev/kvm` added (`container.options: --device /dev/kvm`). A VM inside a job has user-mode networking (outbound
  only, through the job container). Registration token: `/srv/factory/secrets/lab_runner_token_<org>` (root 0600).
  `/dev/kvm` lets a job run guest code with hardware virtualisation; a KVM escape would reach the factory VM, which
  is itself a VM: accepted for small tests of the sandbox; labs with untrusted guests belong on a lab machine.
- **Windows hand (ready to install):**
  [install-windows-hand.ps1](../../infra/hands/windows/install-windows-hand.ps1), run once elevated:
  - Local **standard user `factory-hand`** (Users only; removed from Administrators, Remote Desktop Users, Remote
    Management Users, Hyper-V Administrators, docker-users); random 40-character password set through WMI on the
    service, stored nowhere else; rights: *Log on as a service* only; *denied* local, Remote Desktop, network and
    batch logon. So no RDP, no SMB/WinRM, no interactive use.
  - **NTFS:** explicit inherited **deny full control** for `factory-hand` on each `-DenyPath`
    folder, the owner's source, secrets and profile (by default `D:\projects`, `D:\secrets`, `C:\Users\owner`;
    each separately, in case its ACL does not inherit). `D:\factory-hand`: inheritance from `D:\`
    cut; SYSTEM and Administrators full, `factory-hand` read/execute on `bin` (it cannot replace its own runner) and
    modify on `data`, `work`, `logs`; other users nothing (the runner token is in `data\.runner`).
  - **No Docker, no Hyper-V:** not in `docker-users` (the `docker_engine` pipe refuses it) or Hyper-V Administrators;
    the runner is in host mode (no containers). No firewall rule, no listening port; the runner's cache server is off.
  - Binaries pinned by SHA-256 (act_runner 0.6.1, WinSW 2.12.0); `-Uninstall` removes the service, user, rights,
    deny ACEs and folder.
  - **Signing keys:** the hand cannot read the owner's secrets folder. Release packages are signed by the factory
    signer, not on a hand (D22); no key is granted to the hand.
  - Proof script for when it is installed: [check-isolation.ps1](../../infra/hands/windows/check-isolation.ps1), run
    as a `runs-on: windows` job (runs as `factory-hand`, not admin, cannot list or read the denied paths, cannot
    reach Docker or Hyper-V, runner listens on no port). Run as the owner (unelevated) it fails 9 of 10 checks, so
    the checks discriminate.

## Gate changes (18 Oct 2026)

Gates run from the pushed branch, so an agent could weaken `.gitea/workflows/**` (e.g. a job that only runs
`exit 0`) and get green gates. The supervisor's merge therefore reads the pull request's files: a change to
`.gitea/workflows/`, `.github/workflows/` or a path the product lists under `gates` in `product.yml` (renames
included) is refused with plain `merge` ("⛔ Not merged: this pull request changes the gates …"), and the green-gates
message says so beforehand. The owner merges it only with `merge with gate change: <reason>`; the reason goes into
the merge commit message and the decision log. Onboarding pull requests always need this form.
