# Spec: records (Gitea) and CI

## Purpose

The system of record. The owner never needs to open it; Zulip messages link to it for details.

## Layout

| Gitea | Holds |
|---|---|
| Organisation per product | The product's repositories (mirrored from or to GitHub) |
| Issues | Tasks: request, plan, state labels, links to commits and evidence |
| Pull requests | Each slice's change, reviewed by reviewer agents |
| Repository `factory-records` | Decision log, digests, factory configuration history |
| Gitea Actions | Gates of each product on every push; runners on the factory server and on hands |

## GitHub

GitHub stays the backup and release mirror. Releases are tagged and pushed there by the
release playbook after the owner's approval.

## CI

- Each product declares its gates in its repository (workflow files).
- Linux gates run on the factory server's runner; Windows and lab gates run on runners on the hands (as built F4:
  runners registered to the product's organisation with labels `lab` and `windows`; see
  [architecture.md](../architecture.md#as-built-f4-18-oct-2026)). No runner mounts
  the Docker socket into jobs (`container.docker_host: "-"`).
- A red gate blocks the release playbook.

## As built (F3, 18 Oct 2026)

### Gates

- **Where:** each product repository's `.gitea/workflows/gates.yml` (the product owns it; `product.yml` names it
  under `gates`). Runner `factory-core-1` (act_runner 0.6.1, [runner.yaml](../../infra/core/gitea/runner.yaml)):
  `runs-on: linux` (or `ubuntu-latest`) runs the job in a `node:22-bookworm` container on the VM's Docker, on the
  `factory-gitea_default` network; workflows run on every branch push (`on: push: branches: ['**']`), task branches
  and `main` alike, not on tags (the factory signer's release tags, D22).
- **Default for new products:** `/product new` (and `provision.py` for a repository it creates) commits
  [infra/gates/default-gates.yml](../../infra/gates/default-gates.yml) before `main` is protected: jobs `lint`
  (`make lint`, `npm run lint`) and `test` (`make check`, `make test`, `npm test`, `pytest`) that run the product's
  own command when it exists and otherwise **pass without checking anything**, saying `PLACEHOLDER` in the log.
  Existing repositories are not touched.
- **sandbox:** `sandbox/playground` `.gitea/workflows/gates.yml`: job `text` (tracked `*.txt`/`*.md` end with a
  newline, no trailing whitespace, `hello.txt` not empty if present) and job `shell` (shellcheck on tracked `*.sh`).
- The supervisor reads results through the API (`repos/<repo>/actions/runs?head_sha=<sha>`, `…/runs/<id>/jobs`,
  `…/actions/jobs/<id>/logs`); flow in [supervisor.md](supervisor.md#flow).

### Release (merge)

The owner's `merge` in a task topic merges the task's pull request into the default branch with the supervisor's
admin token (merge commit; the head commit is pinned, the task branch deleted; the issue is closed by `Closes #n`
and by the supervisor). Only with green gates on the pull request's head and no conflicts. Agents' tokens cannot
merge: the default branch's protection has a merge whitelist with only `factory-ops`, the supervisor's admin account
(`provision.py` sets it on every deploy; the agent's token gets 405 "User not allowed to merge PR").
Tags, GitHub and other release targets are not built yet (they are an Owner decision and a later step).

### Decision log

`factory/factory-records` → `decision-log.md`, one table row per owner action (`| When (UTC) | Who | Decision |
[Zulip](link) |`), appended by the supervisor: plan approved (message or ✅), change requested, rejected (message or
❌), cancelled, paused, resumed, merged / merge refused (with the reason), settings set/reset (`/settings`,
`/model default|role`, `/thinking default`, with old and new value), per-topic model and thinking, product
created/removed. New requests are recorded as issues, not in the log. Test runs post as the owner's account; their
lines are marked "TEST, not the owner" afterwards.
