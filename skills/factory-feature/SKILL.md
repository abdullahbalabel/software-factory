---
name: factory-feature
description: Factory playbook for a feature task: new or changed behaviour inside the approved plan.
---

# Playbook: feature

Use for a task of kind `feature`: new or changed behaviour the owner asked for.

1. **Plan** (plan step): restate the behaviour in one line; list the files you expect to touch; say how you will
   check it (a test, a command, a file to read). No file changes in this step.
2. **Build** on branch `task/<n>` only. Keep the change to what the plan says; anything more is new scope and needs
   the owner.
3. **Check** before pushing: run the product's own checks if they exist (`make check`, `npm test`, `pytest`, or the
   steps in `.gitea/workflows/`), and add or update a test for the new behaviour where the product has tests.
4. **Docs:** update the README or docs the change makes wrong, in the same commit.
5. **Push** `git push -u origin task/<n>`; the gates run on the push. When the supervisor sends a red gate's log,
   fix the cause (never weaken a check to pass) and push again.
6. **Reply** with a summary under 80 words: what changed, how you checked it.
