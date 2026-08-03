# Documentation

Everything written down about this system. Plain Markdown, no tooling
required — any AI agent or human reads the same files.

## Where to go

| Directory | What it holds | Who reads it |
|---|---|---|
| **[ai/](ai/)** | **The rules.** Safety, architecture, testing, workflow. | **every agent, every change** |
| [specs/](specs/) | What we are building and why, one file per change | before building |
| [architecture/](architecture/) | How the system is put together | when adding something |
| [operations/](operations/) | Running, releasing, recovering | when it is live |
| [decisions/](decisions/) | Decisions taken and the reasoning | when asking "why is it like this" |
| [history/](history/) | Audit trail of completed work — **read-only** | archaeology |

## If you are an AI agent

Read **[ai/00-start-here.md](ai/00-start-here.md)** first. Then
**[ai/10-golden-rules.md](ai/10-golden-rules.md)**, always, before changing
anything.

This app places real money orders. The rules are strict for that reason and
not negotiable by convenience.

## If you are new here

1. [../README.md](../README.md) — what the app is and how to run it
2. [ai/30-architecture.md](ai/30-architecture.md) — how it is laid out
3. [ai/20-trading-safety.md](ai/20-trading-safety.md) — what can cost money

## Spec-driven development

Anything beyond a one-line fix starts with a spec in [specs/](specs/), copied
from [specs/TEMPLATE.md](specs/TEMPLATE.md).

A spec is half a page and answers four questions: what problem, what changes,
**what must not change**, and how you will know it worked. The third question
is the one that matters most here — most of this system's value is in
behaviour that must stay exactly as it is.

Specs exist because a chat transcript is not a record. Six months from now the
spec is what explains the code.

## A note on `history/`

`history/refactor-2026/` holds 58 work packs from the 2026 restructure. It is
an audit trail: each says what was true at the time, including names of files
that no longer exist.

**Do not update it to match today's code.** Its value is that it records what
was actually done and when. Rewriting it to look current destroys the only
evidence of how the system got here.
