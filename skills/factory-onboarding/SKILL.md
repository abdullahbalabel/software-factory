---
name: factory-onboarding
description: Factory playbook for onboarding a new product: turn the owner's answers into README, real gates, hands and a first backlog, in one pull request.
---

# Playbook: onboarding

Use for an onboarding task (topic `onboarding` of the product's channel). The owner answered five questions in plain
words (what it is, language and stack, build and test, hands, first goals), possibly with attached documents under
`attachments/`. There is no plan step: your pull request is what the owner approves, once its gates are green, with
`merge with gate change: <reason>` (it changes the gates, so a plain `merge` is refused).

## Before you write anything

- Read the answers and any attachments. If you cannot tell the language/stack or how the product would be tested,
  stop: ask short numbered questions in your final reply, push nothing and create no backlog issues. The owner's
  reply comes back to you in the same session.
- Make reasonable assumptions for the rest and list them in your summary (e.g. "Python 3.11, no third-party
  dependencies").

## On branch `task/<n>` (from the default branch)

1. **README.md**: one paragraph on what the product is and for whom; stack; how to build, run and test it (the same
   commands the gates run); where the backlog lives (issues labelled `backlog`). Short and accurate; no marketing.
2. **Project skeleton** (only if the repository has no code yet): the smallest layout the stack expects, with **at
   least one real test** that exercises code you wrote (not `assert True`). Do not implement the owner's goals: they
   are backlog items.
3. **Gates** `.gitea/workflows/gates.yml`: replace the default placeholder jobs (marked `PLACEHOLDER`) with the
   product's real checks, so a broken change turns them red. Keep the trigger `on: push: branches: ['**']` (every
   branch, never tags: the factory signer's release tags `android-<sha>` would otherwise run the gates again).
   - The factory's runner label is `linux`: a `node:22-bookworm` container (Debian 12, Node 22, git, Python 3.11
     without pip or pytest). Install what the job needs inside the job, for example
     `apt-get update -qq && apt-get install -y -qq python3-pytest` for pytest, or `npm ci` for Node.
   - Jobs on hands (`runs-on: windows`, `runs-on: lab`) only if the product declares that hand (step 4) and the
     work needs it.
   - Run the same commands locally before pushing; the gates must be green on your push.
4. **Hands**: call the tool `set_hands` once: `[]` when Linux is enough; `lab` for Linux VM tests, `windows` for
   Windows builds or tests. The supervisor writes them to the product's `product.yml` when the owner merges.
5. **Backlog**: call the tool `add_backlog_item` 3 to 5 times, one per first goal, each small enough for one task:
   a title (under 60 characters) and a body with the goal and how to check it. They are created as issues labelled
   `backlog`, not started; the owner starts one with `/backlog start <n>`.
6. **Commit and push**: `#<n>: onboarding`, `git push -u origin task/<n>`. No pull request (the supervisor opens it).

## Reply

A summary under 120 words: the stack; the gates (job names and the commands they run); the hands; your
assumptions. The supervisor adds the backlog list and the hands to the pull request and the topic.

When the gates are red, the supervisor sends you the failing log: fix the cause (never weaken a gate to pass) and
push again.
