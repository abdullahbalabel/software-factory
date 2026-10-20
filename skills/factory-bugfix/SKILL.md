---
name: factory-bugfix
description: Factory playbook for a bug fix task: reproduce, fix the cause, prove it with a check.
---

# Playbook: bug fix

Use for a task of kind `bugfix`: something behaves wrongly.

1. **Plan:** state the wrong behaviour and the expected one; say how you will reproduce it.
2. **Reproduce first** on branch `task/<n>`: a failing test or a command that shows the bug. If you cannot
   reproduce it, say so in your reply and ask the owner; do not guess a fix.
3. **Fix the cause**, not the symptom; the smallest change that does it.
4. **Prove it:** the reproduction now passes; the product's other checks still pass.
5. **Push** `task/<n>`; fix red gates from the log the supervisor sends.
6. **Reply** under 80 words: the cause, the fix, the proof.
