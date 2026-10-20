---
name: factory-release
description: Factory release (merge) playbook: how a task's pull request reaches the default branch.
---

# Playbook: release (merge)

How a finished task reaches the product's default branch. Agents prepare; only the owner releases.

1. A build ends with the agent's push to `task/<n>`; the supervisor opens the pull request and waits for the gates.
2. Red gates go back to the agent with the failing log (at most `gates.max_fixes` attempts), then to the owner. The
   task is **done** only with green gates on the pull request's head.
3. The owner replies `merge` in the task topic. The supervisor (its own credential, never the agent's) merges only if
   the gates are green on the head and the pull request has no conflicts; otherwise it says why. Each merge or
   refusal is a line in the decision log.
4. Agents never merge, never push to the default branch and never tag or publish. If asked to prepare a release,
   write the release notes (what changed, how it was checked) in the pull request or a file the plan names, and say
   the owner's `merge` is the next step.
5. Publishing beyond the default branch (tags, registries, servers, GitHub) is an Owner decision
   (`docs/decision-policy.md`, Releases) and its own task.
