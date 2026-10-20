---
name: factory-docs
description: Factory playbook for a documentation task: change docs only, keep them true to the code.
---

# Playbook: documentation

Use for a task of kind `docs`: README, guides, comments, help texts.

1. **Plan:** which documents change and why; no code changes in a docs task (if code is wrong, say so and ask).
2. **Write** plain, short, accurate text; check every command, path and name you mention against the repository.
3. **Check:** the product's checks still pass (text and link checks in `.gitea/workflows/` if any); files end with a
   newline, no trailing spaces.
4. **Push** `task/<n>`; fix red gates from the log the supervisor sends.
5. **Reply** under 60 words: what you changed.
