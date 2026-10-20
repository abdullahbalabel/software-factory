"""The owner's commands. This table drives both the dispatcher (core.Supervisor.cmd_<handler>) and /help, so the help
text is always the code's own list."""

# where: task = a task topic (#<n> …), requests = topic "requests", chat = any other topic of stream factory,
# any = all of these.
COMMANDS = [
    {"word": "/help", "handler": "help", "where": "any", "usage": "/help",
     "what": "This list."},
    {"word": "/status", "handler": "status", "where": "any", "usage": "/status",
     "what": "In a task topic: this task. Elsewhere: all tasks (of this product in its stream), queue and model usage."},
    {"word": "approve", "handler": None, "where": "task", "usage": "approve  (or react ✅ on the plan)",
     "what": "Approve the plan; the agent builds it on branch task/<n>, the factory opens a pull request and runs "
             "the gates; red gates go back to the agent (up to `gates.max_fixes` times), then to you."},
    {"word": "change:", "handler": None, "where": "task", "usage": "change: <what to change>",
     "what": "Ask for a revised plan."},
    {"word": "reject", "handler": None, "where": "task", "usage": "reject  (or react ❌ on the plan)",
     "what": "Reject the plan; the task is cancelled and its issue closed."},
    {"word": "/merge", "handler": "merge", "where": "task",
     "usage": "merge · /merge · merge with gate change: <reason> · merge anyway: <reason> · "
              "merge anyway with gate change: <reason>",
     "what": "Merge this task's pull request into the default branch (release playbook): only with green gates on "
             "its head and no conflicts; otherwise it says why. A pull request that changes the gates "
             "(`.gitea/workflows/`, or the product's declared gate files) is refused with plain `merge`, because "
             "gates run from the agent's branch: read the change, then use `merge with gate change: <reason>`. "
             "`merge anyway: <reason>` ships it with red or missing gates (still no conflicts; the gate-change rule "
             "still applies, then `merge anyway with gate change: <reason>`). Recorded in the decision log with the "
             "reason."},
    {"word": "/rework", "handler": "rework", "where": "task", "usage": "rework · rework: <note> · /rework [note]",
     "what": "Send a done or paused task with a pull request back to its agent: it merges the default branch into "
             "its branch (resolving conflicts) and does what the note says; the gates run again, then `merge`."},
    {"word": "/pause", "handler": "pause", "where": "task", "usage": "/pause",
     "what": "Stop this task's agent now; nothing runs until /resume."},
    {"word": "/resume", "handler": "resume", "where": "task", "usage": "/resume",
     "what": "Continue a paused task (also after a time limit or an error)."},
    {"word": "/cancel", "handler": "cancel", "where": "task", "usage": "/cancel",
     "what": "Stop and cancel this task; its issue is closed and its workspace removed."},
    {"word": "/model", "handler": "model", "where": "any",
     "usage": "/model · /model list · /model <name> · /model default <name> · /model role <smol|slow|plan> <name>",
     "what": "Show models; use <name> for this topic's agent from its next turn; set the factory default or a role "
             "model for new sessions."},
    {"word": "/thinking", "handler": "thinking", "where": "any",
     "usage": "/thinking <level> · /thinking default <level>",
     "what": "Thinking level (off, minimal, low, medium, high, xhigh, max) for this topic's agent from its next turn, "
             "or the factory default."},
    {"word": "/settings", "handler": "settings", "where": "any",
     "usage": "/settings · /settings set <key> <value> · /settings reset <key>",
     "what": "Show the agent settings the factory manages; change or reset one (recorded in the decision log)."},
    {"word": "/product", "handler": "product", "where": "factory",
     "usage": "/product list · /product new <name> [title] · /product remove <name>",
     "what": "List products; create one (name: lowercase letters, digits, hyphens): its Gitea organisation and "
             "repository, its stream `<name>`, its agent user and token, live, then the onboarding questions in "
             "its topic `onboarding`; remove one with no open tasks (deletes its repository, stream and agent "
             "user)."},
    {"word": "/onboarding", "handler": "onboarding", "where": "product", "usage": "/onboarding · /onboarding skip",
     "what": "Post the onboarding questions (again) in topic `onboarding`: your answers there become a pull request "
             "with the README, the real gates and the hands, plus 3-5 backlog issues; `merge with gate change: "
             "<reason>` accepts it (it changes the gates). `skip`: no onboarding (the default gates stay)."},
    {"word": "/backlog", "handler": "backlog", "where": "product", "usage": "/backlog · /backlog start <n>",
     "what": "List the product's backlog (open issues labelled `backlog`, not started); start issue <n> as a task "
             "(its own topic: plan, approve, build, gates, merge)."},
    {"word": "/report", "handler": "report", "where": "any", "usage": "/report <what went wrong> · /report list",
     "what": "Report a problem with the factory itself (not a product: product bugs go in `requests` as `Bug: …`). "
             "It is recorded with the time, this topic's link, your attachments and the factory's recent log in "
             "the factory records (label factory-report) and listed in the digest; `list` shows the open reports."},
    {"word": "/digest", "handler": "digest", "where": "any", "usage": "/digest",
     "what": "Post the daily digest now in #factory > digest (tasks per product, waiting decisions, done, model "
             "usage, backups, paused). It is posted every day at `digest.time` (see /settings)."},
]

WHERE = {
    "any": "any topic",
    "task": "task topic",
    "requests": "topic requests",
    "chat": "factory chat topic",
    "factory": "stream factory",
    "product": "a product's channel",
}

HOWTO = """\
**How to use the factory**

*Zulip basics.* A channel is like a folder; a topic is like an email subject: messages with the same topic form one \
conversation. The **Inbox** shows every topic with unread messages; tap a channel to see all its topics.

*Answering on the phone (the most important part).*
- To answer a plan or a question, **tap the notification** (or open the task's topic `#<n> …` in the product's \
channel) and type your reply at the bottom of **that** topic. Example: in topic `#3 Exam simulator` type `approve`.
- **Every message in topic `requests` becomes a new task.** Write one message per job there; follow-ups, answers and \
approvals go in the task's own topic. (If you type `approve` elsewhere, the factory applies it when only one task is \
waiting, or lists the waiting tasks to choose from.)
- Type topic names without `/` (e.g. `requests`, `onboarding`, `chat`); commands start with `/`, topics do not.
- This guide is always in topic `how to use` of each channel, and `/help` shows it in any topic.

*Channels.*
- `factory`: chat with the factory's assistant, the daily digest (topic `digest`) and anything across products.
- One channel per product (e.g. `{default}`): that product's requests and tasks. Create a product with \
`/product new <name> <title>`; it gets its channel, its Gitea project and its own agent, and asks you a few \
questions in its topic `onboarding` (what it is, stack, build and test, hands, first goals). Answer in plain words; \
its agent prepares the README, the gates and 3-5 backlog issues as one pull request; read it and reply \
`merge with gate change: <reason>` to accept (it sets up the gates). \
`/backlog start <n>` turns a backlog issue into a task.

*Working, step by step.*
1. **Talk:** start any topic in `factory` (e.g. `chat`) and write normally: ask about status, tasks or decisions, \
discuss ideas, ask for research. When you are happy, write "make this a task".
2. **Or ask directly:** write the job in topic `requests` of a product's channel (or of `factory` for {default}).
3. **Plan:** a task topic `#<n> <title>` opens in the product's channel and the agent posts a plan there (you get a \
notification). Reply `approve`, `change: <what>` or `reject` (or react ✅ / ❌).
4. **Build and checks:** the agent builds; the gates run on every push (✅ / ❌ in the topic). Red gates go back to \
the agent (up to `gates.max_fixes` tries), then it asks you.
5. **Merge:** with green gates reply `merge`; it merges only when the gates are green. If the pull request changes \
the gates themselves (onboarding always does), read that change and reply `merge with gate change: <reason>`. To \
ship with red gates on purpose: `merge anyway: <reason>`. If it conflicts with the default branch (or needs a \
touch-up after it was done), reply `rework` or `rework: <note>`: the agent updates it and the gates run again.

*Good to know.*
- In a task topic you can always write to its agent, or use `/pause`, `/resume`, `/cancel`, `/status`.
- Files and images you attach (requests, task topics, chat) reach the agent in its workspace under `attachments/` \
(up to {max_mb} MB each; types: {exts}).
- Model usage: the factory pauses new work when the 5-hour or 7-day usage window of the model account passes its \
threshold (`/settings`), says so in the topic and continues after the reset. `/status` and `/digest` show both.
- Phone notifications come for plans, questions, results and the digest. If none arrive, check Android's Do Not \
Disturb and Zulip's battery setting.
- Only your account (organisation owner) can give commands or approve; every decision is logged."""


def find(word):
    for c in COMMANDS:
        if c["handler"] and c["word"] == word:
            return c
    return None


def help_text(default_product, attachments):
    lines = [HOWTO.format(default=default_product, max_mb=attachments["max_mb"],
                          exts=", ".join(attachments["extensions"])), "", "**Commands**"]
    for c in COMMANDS:
        lines.append(f"- `{c['usage']}` — {WHERE[c['where']]}: {c['what']}")
    return "\n".join(lines)
