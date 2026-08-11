# Documentation

Everything written down about this system. Plain Markdown, no tooling
required — any AI agent or human reads the same files.

## Where to go

| Directory | What it holds | Who reads it |
|---|---|---|
| **[system/](system/)** | **The knowledge base.** Goal, rules, and one living directory per part of the system. The single point of truth. | **every agent, every change** |
| [specs/](specs/) | What we are building and why, one file per change | before building |
| [todo/](todo/) | Multi-session work packs in progress | while executing a plan |
| **[questions/](questions/)** | **Deferred decisions** — the answer-later queue (see below) | when a decision can wait |
| [reviews/](reviews/) | Point-in-time review snapshots | when auditing |
| [todo/refactor/stage0/](todo/refactor/stage0/) | Audit trail of the 2026 refactor (was docs/history/refactor-2026/) — **read-only** | archaeology |

## If you are an AI agent

Read **[system/rules/00-start-here.md](system/rules/00-start-here.md)** first.
Then **[system/rules/10-golden-rules.md](system/rules/10-golden-rules.md)**,
always, before changing anything.

This app places real money orders. The rules are strict for that reason and
not negotiable by convenience.

## If you are new here

1. [system/vision/000-goal.md](system/vision/000-goal.md) — what this system is and what it is for
2. [../README.md](../README.md) — how to run it
3. [system/rules/30-architecture.md](system/rules/30-architecture.md) — how it is laid out
4. [system/rules/20-trading-safety.md](system/rules/20-trading-safety.md) — what can cost money

## The knowledge base — `system/`

`system/` is a living game plan: [system/vision/](system/vision/) says why the
system exists, [system/rules/](system/rules/) says what must never be
violated, and [system/domains/](system/domains/) holds one directory per part
of the system — its constraints, known behaviours, and open questions.

These files are **updated as we learn**. When you discover a non-obvious
behaviour, settle a question, or add a constraint, record it in the relevant
domain file in the same change. Code explains *how*; `system/` explains
*what and why*.

## Spec-driven development

Anything beyond a one-line fix starts with a spec in [specs/](specs/), copied
from [specs/TEMPLATE.md](specs/TEMPLATE.md).

A spec is half a page and answers four questions: what problem, what changes,
**what must not change**, and how you will know it worked. The third question
is the one that matters most here — most of this system's value is in
behaviour that must stay exactly as it is.

Specs exist because a chat transcript is not a record. Six months from now the
spec is what explains the code. When a spec ships, fold what it taught us
back into the relevant `system/domains/` file.

## Deferred decisions — `questions/`

Not every decision has to be made before the work. Some genuinely can be settled
*after*, once the system is built and running. Those live in
[questions/](questions/), the answer-later queue.

The working method:

1. **Keep moving** — work does not stall waiting for an answer.
2. **Decide provisionally** — where a decision is needed to proceed, a sensible,
   safe default is chosen and the system is built to run on it.
3. **Make sure it runs** — every provisional default is one the app works under
   today (green suite, boots, safe on demo).
4. **Hand the queue over** — the owner's brother (who holds the trading and
   business calls) reviews [questions/](questions/) in one pass and confirms or
   overrides each. An answered question is annotated, never deleted.

A provisional default is never a silent one: each file in `questions/` records
what was chosen, why, what it touches, and what changes if the answer differs.
And confirming a default there is a *decision* — **not** the owner sign-off + demo
session that any money-path (order placement, closing, sizing) change still
requires before it ships.

## A note on the audit trail (`todo/refactor/stage0/`)

`todo/refactor/stage0/` (formerly `docs/history/refactor-2026/`) holds 58 work packs from the 2026 restructure. It is
an audit trail: each says what was true at the time, including names of files
that no longer exist — and links to `docs/ai/`, which has since moved to
`docs/system/rules/`.

**Do not update it to match today's code.** Its value is that it records what
was actually done and when. Rewriting it to look current destroys the only
evidence of how the system got here.
