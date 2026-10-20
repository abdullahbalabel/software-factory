# Spec: OMP runtime

## One installation

OMP is installed **once, on the factory server**. Its home (memory, skills, sessions, settings, logins) is one
persistent volume, backed up nightly. No other machine runs OMP.

## Per product

- An OMP **profile** per product (`--profile <product>`, …): its own sessions, settings and memory, so products do not
  mix. Shared skills (general playbooks, the owner's preferences) are available to all profiles.
- Product skills (playbooks) live in the product's repository or in this repository under `products/<name>/skills/`.

## Per task

- A **workspace** per task: a clean checkout of the product's repositories (worktree or container) with the
  product's tools. OMP runs the agent with `--cwd` set to it. Removed when the task ends.
- Headless runs get stdin from `/dev/null` (`ssh -n`, `</dev/null`); with an open stdin pipe `omp -p` waits for piped
  input and never starts.
- Sessions run headless: `omp --mode rpc --no-ui --profile <product> --cwd <workspace>`; resumed with
  `--resume <session file>` (`--restore` requires an interactive terminal in 18.10.3); bounded with `--max-time`;
  permissions with `--approval-mode`.
- As built (F2): one root-owned OMP install in `/opt/factory-omp`; sessions run as unprivileged agent users
  (`agent-<product>`, `agent-chat`), each with its own OMP home; the model login is held by the auth broker
  (`omp auth-broker serve`, 127.0.0.1:8765) and agents are its clients. Workspaces:
  `/srv/factory/workspaces/agent-<product>/task-<n>` (fresh clone; removed on cancel). See
  [supervisor.md](supervisor.md#as-built-f2-18-oct-2026) and [security.md](security.md#as-built-f2-agent-isolation).
- Subagents (parallel slices) run inside the same OMP installation, as today.

## Reaching hands

Agents run commands on hand machines over Tailscale (SSH, or a Gitea Actions job targeted at that machine) and read
the results back. Hands hold no OMP state.

## Logins

See [model-access.md](model-access.md).
