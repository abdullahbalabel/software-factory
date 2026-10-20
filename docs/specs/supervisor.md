# Spec: supervisor

## Purpose

A small, always-running service on the factory server that connects Zulip, Gitea and OMP. It does no coding.

## Responsibilities

1. **Intake:** turn a Zulip request into a Gitea task (issue) in the product's repository; open the task's topic.
2. **Run:** start an OMP session for the task's current station (plan, build, review, release) in the task's
   workspace, with the product's OMP profile and playbook; one session per task; limited concurrency per product.
3. **Bridge:** forward the agent's messages to the task topic and the owner's replies to the session
   (`omp --mode rpc`).
4. **Gate:** stop at owner approvals and Owner rows of the [decision policy](../decision-policy.md); wait without
   blocking other tasks.
5. **Pace:** read `omp usage` and pause work before a usage limit; resume when it resets; owner priorities first.
6. **Recover:** after a restart or crash, resume each interrupted session (`omp --resume <session file>` with a
   "continue the step" prompt; `--restore` needs a terminal in OMP 18.10.3); report what happened.
7. **Record:** write state changes, approvals and results to Gitea (task comments, labels, decision log).
8. **Report:** daily digest at the owner's chosen time; weekly summary per product.

## Task states

`new → planning → awaiting-plan-approval → building → in-review → verifying → awaiting-release-approval → released`
plus `paused (reason, until)`, `blocked (question)`, `cancelled`. Stored as Gitea labels; the supervisor's own state is
rebuilt from Gitea after a restart.

## Technology

A small service (Python or TypeScript) with: Zulip bot API, Gitea REST API, OMP RPC mode. Configuration and product
registry in this repository.

## As built (F2, 18 Oct 2026)

Code: [supervisor/](../../supervisor/) (Python 3.12, standard library + PyYAML). Install: [infra/supervisor/](../../infra/supervisor/).
Security model: [security.md](security.md#as-built-f2-agent-isolation).

### Deployment

- **systemd service `factory-supervisor`** on the factory VM (user `factory-supervisor`, `Restart=always`, enabled at
  boot; logs in journald and `/var/log/factory-supervisor/{supervisor,agents}.log`; state in
  `/var/lib/factory-supervisor/`). Not a Compose container: each agent session must run as a separate unprivileged
  Unix user with its own OMP home; from a container that needs a root process switching users inside it and the OMP
  install and homes duplicated into images and volumes. The service starts agents only through
  `sudo -u agent-<…> /usr/local/sbin/factory-agent-exec`.
- **systemd service `factory-omp-broker`**: `omp auth-broker serve` on 127.0.0.1:8765, the only holder of the
  model login; every agent session (any user, any profile) gets access tokens from it.
- OMP 18.10.3 is installed once, root-owned, in `/opt/factory-omp`.
- Deploy: push this repository to GitHub and to its Gitea copy `factory/software-factory`, then on the VM
  `sudo bash /opt/factory/software-factory/infra/supervisor/update.sh` (pull, `provision.py`, `install.sh`).
  First time only: `import-login.sh` copies the owner's model login into the broker.

### Flow

| Owner does (Zulip) | Supervisor does |
|---|---|
| Writes in topic `requests` of stream `factory` (product `sandbox` by default) or of a product stream | Creates issue `#n` (labels `factory-task`, `planning`) in the product repository; replies with the link; opens topic `#n <title>` in the product stream with the request; queues the **plan** step |
| — | Plan step: `omp --mode rpc --no-ui --profile <product> --cwd <workspace> --approval-mode yolo --max-time 15m --model … --thinking … --append-system-prompt <file>` as `agent-<product>` (the system prompt is a 0600 file in the agent's home, removed once the session answers; never on the command line), in a fresh clone `/srv/factory/workspaces/agent-<product>/task-<n>`; the prompt asks for a short plan only, with a line `Kind: feature\|bugfix\|docs\|refactor` (the playbook; default feature). Posts the plan (also as an issue comment), mentions the owner and asks `approve` / `change: …` / `reject` or ✅ / ❌; label `awaiting-approval` |
| `approve` or ✅ | Records the approval (issue comment, decision log); label `building`; **build** step in the same session (`--resume <session file>`), told to follow the playbook skill `factory-<kind>`: branch `task/<n>`, commit, push. The supervisor then opens the pull request, posts the summary with its link, label `checking`, and **waits for the gates** (below) |
| — | **Gates** (F3): every 15 s the supervisor reads the Gitea Actions runs of the pushed head commit (`actions/runs?head_sha=`, latest run per workflow). All completed and successful: "✅ Gates green" with links, label `done`, the owner is mentioned (`merge` to merge), workspace removed. Any failed: "❌ Gates red" with links, the failing jobs' failed steps and the last 40 log lines (timestamps and runner noise removed, ≤ 2,500 characters per job, at most 2 jobs) as an issue comment, and a **fix** step in the same session with that excerpt (label `building`); the next push is checked again. After `gates.max_fixes` (default 2) fix attempts the task pauses and the owner is asked; his messages then go into the next fix attempt on `/resume`. No run and no workflow file in the commit after 90 s: "no gates declared", `done` but not mergeable from Zulip. No result within `gates.timeout` (default 30 min): paused, owner asked. A reply without a new commit while red: posted with "no new commit". Messages in the topic while gates run are not forwarded (the topic says so) |
| — | **Hands** (F4): the agent's system prompt lists the hands its product declares in `product.yml` (`hands: [lab]`, `[windows]`; known hands and what agents are told about them in `config.yml` `hands`), or says there are none; the plan prompt asks for a work step that needs Windows or a VM to be planned as a gates job on a hand (`runs-on: <label>`), or named as a risk. Agents never reach a hand; they push the job. Gate results add one line per hand job: "🖐 Hand `lab`: job `lab` ✅ success on `lab-sandbox`". A run still pending after 2 minutes is checked once for jobs no online runner serves (instance runners plus the product organisation's): "⏸ Gates on `<sha>`: job `x` waits for hand `windows`: its runner is offline or not installed" (or "needs hand `windows`, which this product does not declare", or "has `runs-on: foo`, which no runner serves"); then `gates.timeout` applies as before ([security.md](security.md#hands)) |
| `merge` (or `/merge`) | **Release playbook:** only for a `done` task; reads the pull request: open, head commit's gates green (read again, not from the cached result), `mergeable` (no conflicts), and its files: a change to `.gitea/workflows/`, `.github/workflows/` or a `product.yml` `gates` path is refused unless the owner wrote `merge with gate change: <reason>` (gates run from the agent's branch; [security.md](security.md#gate-changes-18-oct-2026)). Then merges it with the supervisor's admin credential (merge commit, `head_commit_id` pinned to the checked head, task branch deleted; agents never merge), label `merged`, issue closed, decision log line (with the reason). Otherwise "⛔ Not merged: <reason>" (red gates with links and the `merge anyway` hint, gates still running, no gate results, conflicts with the `rework` hint, gate changes, task not done), also recorded |
| `merge anyway: <reason>` (D17) | Same, for a task with a pull request in any state but `merged`/`cancelled` and no agent running: merges despite red, missing or unfinished gates; a non-empty reason is required; no conflicts still required; with gate changes the form is `merge anyway with gate change: <reason>`. Queued or held steps are dropped. The topic says "⚠ Merged with **red** gates … on your `merge anyway`: <reason>"; the decision log and the merge commit carry the reason and the run links |
| `rework` / `rework: <note>` (D17) | For a `done` or `paused` task with a pull request: label `building`, fix attempts reset, a **rework** step in the same session (prompt `REWORK`: merge the default branch into `task/<n>`, resolve conflicts, the note, push; no rebase or force-push); then the gates and `merge` as usual. Recorded |
| `change: …` | Label `planning`; the session revises the plan |
| `reject` or ❌ | Label `cancelled`; issue closed; workspace removed |
| Any other message in a task topic | Steered into the running session, or queued as the session's next prompt; the agent's reply comes back to the topic (over 1,500 characters: an issue comment with a link). Agents can post a question or progress line themselves with the host tool `post_to_topic` |
| Any other topic of stream `factory` | **Chat:** one OMP session per topic (`agent-chat`, profile `factory`, resumed across restarts), working directory a read-only clone of `factory/software-factory`, a read-only Gitea token, host tools `factory_status` and `create_task` (only on the owner's explicit request: same intake as `requests`) |

Commands (the table in [commands.py](../../supervisor/factory_supervisor/commands.py) drives both the dispatcher and
`/help`, and the chat assistant's system prompt): `/help`, `/status`, `/pause`, `/resume`, `/cancel`, `/merge`,
`/model …`, `/thinking …`, `/settings …`, `/product …`, `/onboarding …`, `/backlog …`, `/digest`, plus `approve` /
`change:` / `reject` / `merge`. `/onboarding` and `/backlog` work in a product's channel (F5).
Only messages and reactions of organisation owners (role 100) are acted on; bots and other members are ignored.

### Playbooks (F3)

The general playbooks are OMP skills in this repository, [skills/](../../skills/): `factory-feature`,
`factory-bugfix`, `factory-docs`, `factory-refactor`, `factory-release` (merge), `factory-onboarding` (F5).
`factory-product-admin sync` (every `update.sh`, and `/product new`) copies them, as each agent user, into its OMP
profile's user-level skills directory
`~/.omp/profiles/<profile>/agent/skills/` and removes `factory-*` skills no longer in the repository. The planner
states the kind (`Kind: …` line, kept with the task); the build prompt names `skill://factory-<kind>`; the task system
prompt lists the playbooks. Product-specific playbooks are not built yet (products.md).

### Products from Zulip (`/product`, D16)

In stream `factory`: `/product list`; `/product new <name> [title]` (name: 2–20 characters, lowercase letters, digits,
single hyphens, starting with a letter; not reserved, not existing); `/product remove <name>` (not the default
product, no open tasks). The supervisor runs the root helper
[factory-product-admin](../../infra/supervisor/factory-product-admin) (one sudo rule; it validates every argument):

- **new:** writes `products/<name>/product.yml` in the deployed checkout (org and repository `<name>/<name>`, agent
  account `<name>-agent`, Unix user `agent-<name>`, profile and stream `<name>`); provisions Gitea like
  `provision.py` (repository with README, protected `main`, agent account with a `write:repository` token for that
  repository only, `factory-reader` read access); creates the Unix user and its workspace directory; rewrites the
  sudo rule; stores the token in the agent's home. The supervisor then commits the same file to the Gitea copy
  `factory/software-factory` (admin token), creates the public stream `<name>` with the owner subscribed, ensures
  the state labels and adds the product to its live registry: no restart, no deploy. Then it posts the onboarding
  questions in the product's topic `onboarding` (below).
- **remove:** deletes the Gitea repository, organisation, agent account and token file, the Unix user with its home
  and workspaces, the product file (also in Gitea), and archives the stream; the product's tasks leave the state.
- **GitHub:** the registry commit lands in Gitea only. Workstation sync before the next push:
  `git pull --ff-only gitea main` then `git push origin main` (pushing to Gitea without it is refused as
  non-fast-forward, so nothing is lost).
- **hands** (F5): `factory-product-admin hands <name> [hand…]` sets `hands` in the product's file (only hands known
  in `config.yml`), and runs the lab hand installer when `lab` was added or removed; the supervisor commits the file
  to the Gitea copy and updates its live registry. Used when the owner merges an onboarding pull request.

### Onboarding (F5)

Flow and rules: [products.md](products.md#onboarding-built-f5-18-oct-2026). In the supervisor: the owner's
first message in a product's topic `onboarding` (while onboarding is not `skipped` or `accepted`) creates the
onboarding task (issue `Onboarding`, labels `factory-task` + `building`, topic `onboarding`, kind `onboarding`) and
queues its build step with the prompt `ONBOARD` (prompts.py) and two extra host tools: `set_hands` (hands known in
`config.yml`; kept with the task) and `add_backlog_item` (at most 5, duplicates by title refused; the supervisor
creates the issue with label `backlog` using its admin token). No branch pushed after the turn: the agent's
questions go to the topic and the owner's reply is forwarded. A push: the pull request (agent summary plus hands and
backlog) and the gates as for any task; green asks for `merge with gate change: <reason>` (the pull request changes
the gates); the merge is as for any task, then writes the
hands (above) and marks onboarding `accepted`. `/backlog start <n>` makes a backlog issue a task: topic marker in
its body, labels `factory-task` + `planning` (the `backlog` label goes), plan step queued. Onboarding state per
product (`asked`, `running`, `accepted`, `skipped`) is in `state.json`.

### Daily digest

Posted in `factory > digest` every day at setting `digest.time` (default `08:00`) in `digest.timezone` (default
`Asia/Riyadh`), both changeable with `/settings set`; `/digest` posts one now ("on request"; it does not replace the
scheduled one). No model use: tasks by state per product, plans waiting for approval (topic and issue links), tasks
finished since the previous scheduled digest, paused tasks and pacing holds, model usage (`omp usage`), the VM's
nightly backup (`systemctl show factory-backup.service`) and the last off-site copy as reported by the owner's PC
(`pull-backup.py` writes its result to `/var/lib/factory-offsite/last-pull` over SSH). A day missed while the
supervisor was down is posted when it starts; on its very first start after the time, the first digest is the next
day's. Changing `digest.time` to a later hour on a day whose digest was already posted takes effect the next day.

### Attachments

Files and images the owner attaches (Zulip `/user_uploads/…` links) in `requests`, a task topic or a chat topic are
downloaded with the bot's API key (temporary URL), at most `attachments.max_mb` (25 MB) each and only the extensions
in `attachments.extensions` (config.yml); others are refused in the topic with the allowed list. Accepted files are
written as the agent user (`factory-agent-exec put-file`) to `<workspace>/attachments/<message id>-<name>` (0600,
excluded from git) and their paths are appended to the agent's message (for a request: to the task's request text).
Task workspaces are created at intake for this; chat uses its `factory` clone.

### Settings from Zulip

Defaults in [supervisor/config.yml](../../supervisor/config.yml) (`defaults`). The owner's changes (`/model default`,
`/model role`, `/thinking default`, `/settings set|reset`) are validated (models against `omp models --json`), stored
in Gitea `factory/factory-records` → `settings/overrides.json`, and logged with old and new value in its
`decision-log.md`. Per-topic choices (`/model <name>`, `/thinking <level>` in a task or chat topic) are kept with the
task or chat. All are applied as `omp` flags (`--model`, `--thinking`, `--smol/--slow/--plan`, `--max-time`) when the
topic's next step starts. Not changeable from chat: agent users, tokens, approval mode, sandboxing.

### State, restart and resume

- Gitea is the record: issue labels (`planning`, `awaiting-approval`, `building`, `checking`, `done`, `merged`,
  `paused`, `cancelled`), an
  issue-body marker with the topic, a comment with the session file. `state.json` adds the job queue, steps in flight
  and topic links; with it lost, tasks are rebuilt from Gitea. A task in `checking` keeps waiting for its gates after
  a restart (pushed head, fix attempts and result are in `state.json`; rebuilt from the branch head if lost).
- On SIGTERM the supervisor stops its agent processes, leaves the running step marked in flight and exits. On start it
  first stops any agent process left from the previous run (a crash, or one systemd could not reach: agents run in
  sudo sessions), then re-queues every interrupted step and says so in its topic; the step resumes the same OMP
  session (`--resume <session file>`) with a prompt to check the workspace and finish the step.
  `omp --restore` is not usable here: OMP 18.10.3 accepts it only with an interactive terminal.
- Owner messages sent while the supervisor was down are fetched on start (from the last handled message id).

### Pacing

Before each step the supervisor reads `omp usage --json` (as `agent-chat`, through the broker; cached two minutes).
It checks two windows: the 5-hour window against `pace_threshold` (default 70 %) and the 7-day window against
`pace.weekly_threshold` (default 80 %; added 18 Oct 2026). When either
is at or above its threshold the step waits: the topic gets "⏸ Waiting before the next step: model usage is at X %
of the **7-day** window (factory limit Y %). That window resets <time>; the step continues automatically once usage
is below the limit." (Zulip shows the time in the owner's time zone) and a task gets label `paused`; it re-checks
every 5 minutes or at the reset, then posts "▶ Continuing: model usage is now …" with both windows and runs the
step. `/status` and the digest show both windows (share, factory limit, reset). A model error that reports a usage
or rate limit re-queues the step the same way. `/settings set pace_threshold 0%` holds all new steps. Concurrency:
one agent step at a time (setting `concurrency`, 1–3).

### Not yet (later steps)

Weekly summary (F2 remainder), review and verify stations (reviewer agents), release beyond the default branch
(tags, GitHub mirror, release targets), product-specific playbooks; the Windows hand's install (F4, waits for the
owner).
