# Spec: cockpit (Zulip)

## Purpose

The owner's only interface, on Android phone and tablet (Zulip app) and laptop (Zulip desktop app or browser).

## Layout

| Zulip | Use |
|---|---|
| Stream `factory` | Topic `digest`: the daily digest; topic `requests` (default product); **any other topic is a chat with the factory's assistant** (one OMP session per topic: questions about status, tasks, decisions, plans; ideas, research; turns a discussion into a task when asked); `/product` commands |
| Stream per product (`sandbox`, …; created by `/product new`) | All work of that product |
| Topic per task (`#123 Add CSV export`) | The whole conversation of one task: plan, progress, questions, results. **Replying in the topic talks to the task's agent.** |
| Topic `requests` in `factory` and in each product stream | New requests; the supervisor answers with the new task's topic |
| Direct messages from the bot | Only urgent items (release blocked, security issue, login expired) |

## Commands (typed as messages)

As built in F2 (the authoritative list is `/help`, generated from the supervisor's command table):

| Command | Where | Effect |
|---|---|---|
| any message in `requests` (optionally starting `/request`) | topic `requests` | New task |
| `approve`, `change: <text>`, `reject` (or ✅ / ❌ reactions on the plan) | task topic | Answers the pending plan approval |
| `merge` or `/merge` | task topic | Release playbook (F3): merges the task's pull request into the default branch, only with green gates on its head and no conflicts; otherwise says why. Recorded in the decision log |
| any other message | task topic | Goes to the task's agent; its reply comes back (not while the gates run: the topic says so) |
| any message | other topic of `factory` | Chat with the factory's assistant |
| `/help` | anywhere | Every command, and how to request, approve, change, reject, chat |
| `/status` | anywhere | In a task topic: that task. Elsewhere: all tasks (of the product, in its stream), queue, model usage |
| `/pause`, `/resume`, `/cancel` | task topic | Controls that task |
| `/model`, `/model list`, `/model <name>`, `/model default <name>`, `/model role <smol\|slow\|plan> <name>` | anywhere (`<name>` alone: task or chat topic) | Show or choose the agent model: for this topic from its next turn, or the factory default / role model for new sessions |
| `/thinking <level>`, `/thinking default <level>` | anywhere | Thinking level for this topic, or the default |
| `/settings`, `/settings set <key> <value>`, `/settings reset <key>` | anywhere | Managed agent settings (model, thinking, roles, time limits, concurrency, pacing threshold, digest time and time zone, `gates.max_fixes`, `gates.timeout`); changes are validated and written to the decision log |
| `/product list`, `/product new <name> [title]`, `/product remove <name>` | stream `factory` | List products; create one live (Gitea repository with a default gates workflow, stream, agent user and token); remove one with no open tasks |
| `/digest` | anywhere | Posts the digest now in `factory > digest` (also posted daily at `digest.time`, default 08:00 Asia/Riyadh) |
| a file or image attached to a message | `requests`, task topic, chat topic | Copied into the agent's workspace (`attachments/`), path given to the agent; up to 25 MB, listed types only (others refused with a message) |

## Notifications

- Push only for mentions of the owner (decisions, approvals, urgent items); everything else is silent.
- Zulip Server 12.0 or newer with **Require end-to-end encryption for push notifications** on; Android app 30.0.271+.
- Self-hosted Free plan: push for up to 10 users.

## Rules

- No secrets, keys or customer data in messages; links to Gitea for details.
- Every approval is written to the decision log with who, when and what.
- Only the owner's account can approve; bots cannot approve.
