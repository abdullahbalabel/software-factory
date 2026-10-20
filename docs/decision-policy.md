# Decision policy

Agents act alone inside the approved plan. Anything below marked **Owner** stops the task and asks in Zulip; the task
waits, other tasks continue.

| Area | Agents decide alone | Owner decides |
|---|---|---|
| Scope | Implementation details within the approved plan | New scope, dropping scope, priorities between tasks and products |
| Code | Design inside a slice, refactors needed for the slice, tests | Changes to public contracts or data formats that customers depend on |
| Quality | Fixing failing gates and review findings | Shipping with a known failing gate or an open high/critical finding |
| Security | Applying fixes, hardening that changes no behaviour | Security trade-offs, accepting a risk, anything touching keys or signing |
| Releases | Preparing a release candidate and its notes | Publishing a release; deploying to anything customer-facing or shared |
| Money | — | Any spending: servers, services, licences, paid plans |
| External | — | Messages to customers or third parties; publishing anything public |
| Credentials | Using credentials already given to the factory | New credentials, logins, passkeys, ceremonies |
| Data | Test data | Real customer or client data |

## How questions reach the owner

- Batched, at most a few times a day, plus immediately for anything blocking a release or a security issue.
- Each question: one sentence of context, the options with their consequences, the agent's recommendation, and the
  default if the owner does not answer by a stated time (only for low-risk questions; never for the Owner rows above).
- The answer is recorded in Gitea's decision log and in OMP memory.
