# Vision

## Problem

Building products with AI agents works, but it depends on the owner being present at a computer and on one long
conversation:

- state lives in chat and hand-written resume notes, so a restart or a usage limit needs manual recovery;
- decisions are asked for ad hoc, at any hour;
- quality gates run when an agent remembers to run them;
- one PC is shared by builds, labs and the owner's own work, so they block each other.

The owner needs to focus on other work and still lead several products.

## Goal

A factory that runs on its own and stops for the owner only at defined points:

- **One interface.** The owner uses one app (Zulip) on any device. No terminal, no SSH, no consoles.
- **About 20–30 minutes a day.** Read the digest, approve or adjust plans and releases, answer waiting decisions.
- **Durable.** Every task's state is saved outside chat; restarts and usage limits pause work, never lose it.
- **Automatic quality.** Gates run on every change; a red gate blocks a release.
- **Product-neutral.** Products are tenants with their own code, channels, playbooks, machines and secrets.
- **Secure.** Everything private and reachable only over the tailnet; secrets never in chat.

## What stays with the owner, on purpose

Product scope and priorities, plans and releases, spending money, anything customer-facing, security trade-offs,
passkeys and signing ceremonies.
