# Spec: products

Products are tenants of the factory.

## A product has

| Item | Where |
|---|---|
| Repositories | Gitea organisation of the product (and GitHub) |
| Zulip stream | `<product>` |
| OMP profile | `<product>` |
| Playbooks | General playbooks ([skills/](../../skills/), installed into the agent's OMP profile; built F3) + `products/<product>/skills/` (not built yet) |
| Gates | `.gitea/workflows/gates.yml` in its repository (a default with placeholders is seeded by `/product new`, built F3; replaced by the product's real checks in onboarding, built F5) |
| Hands | `hands:` in `product.yml`: the machines whose runners take its gates jobs (`lab`, `windows`; the known hands and what agents are told about them are in `supervisor/config.yml`). Each hand's runner is registered to the product's Gitea organisation. sandbox: `[lab]` (built F4) |
| Secrets | Its own, never visible to other products |
| Release targets | Where releases go (registries, servers, public copies) |

## Onboarding (built F5, 18 Oct 2026)

A new product is set up from Zulip, with no change to the factory's code or configuration:

1. **Create** (`/product new <name> <title>` in stream `factory`): registry file `products/<name>/product.yml`,
   Gitea organisation and repository (README, default gates, protected `main`), agent account and token, Unix
   agent user and OMP profile with the playbooks, Zulip stream (F2).
2. **Questions**: the supervisor posts five questions in the product's topic `onboarding` (what it is, language and
   stack, build and test, hands, first goals) and mentions the owner.
3. **Answers**: the owner answers there in plain words (one message; attachments go to the agent). The answer
   starts an **onboarding task** (issue `Onboarding`, state `building`; no plan step: the answers are the brief and
   the pull request is what the owner approves). Its agent (the product's agent user) follows the playbook
   [skills/factory-onboarding](../../skills/factory-onboarding/SKILL.md): README.md; the smallest project skeleton
   with at least one real test when the repository is empty; `.gitea/workflows/gates.yml` with the product's real
   checks instead of the placeholders; host tool `set_hands` (the hands it needs); host tool `add_backlog_item`
   3-5 times (issues labelled `backlog`, created by the supervisor, not started; the agent's token cannot create
   issues). If the answers are not enough to pick the stack or the checks, the agent asks instead of pushing, and
   the owner's reply in the topic goes back to it.
4. **Pull request**: the supervisor opens it with the agent's summary, the hands and the backlog list, waits for
   the gates (red gates go back to the agent as for any task) and asks for `merge`.
5. **Accept**: `merge` (only with green gates, as for any task) merges it; then the hands are written to
   `product.yml` (`factory-product-admin hands`: deployed checkout, which also starts or stops a lab runner when
   `lab` changes; the supervisor commits the file to the Gitea copy and updates its live registry). Onboarding is
   `accepted`.
6. **Work**: `/backlog` lists the backlog, `/backlog start <n>` turns an issue into a task (own topic, plan →
   approve → build → gates → merge); new requests go in topic `requests`.

`/onboarding skip` (in the product's channel) ends onboarding without it (an open onboarding task is cancelled; the
default gates stay); `/onboarding` asks the questions again. `/product list` shows each product's onboarding state
(`asked`, `running`, `accepted`, `skipped`). Kept in the supervisor's state: the hands the agent chose and the
backlog list of an open onboarding task (lost with `state.json`: the task is rebuilt from Gitea, the hands then
stay as they are in `product.yml` and the backlog issues stay in Gitea).

Not built: product-specific playbooks (`products/<product>/skills/`), release targets, and importing an existing
product's knowledge (build and release procedures, known issues) as skills and memory: that belongs to onboarding
existing products (below).

## Existing products

Importing existing products is a separate project, planned later. The factory proves onboarding on a small new
sample product (step F5).

