---
name: factory-refactor
description: Factory playbook for a refactor task: change structure, keep behaviour identical.
---

# Playbook: refactor

Use for a task of kind `refactor`: restructure code without changing what it does.

1. **Plan:** what moves or is renamed, and why; how you will show behaviour is unchanged (existing tests, or a test
   you add before refactoring).
2. **Build** on `task/<n>` in small steps; update every caller; remove code the refactor makes dead. No new
   features, no behaviour changes (those need the owner).
3. **Check:** the same tests pass before and after; the product's checks pass.
4. **Push** `task/<n>`; fix red gates from the log the supervisor sends.
5. **Reply** under 80 words: what changed in structure, and the proof behaviour did not.
