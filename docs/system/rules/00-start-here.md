# Start here

You are working on a **live FOREX trading application**. It connects to a real
MetaTrader 5 account and places real orders with real money.

That single fact drives everything else on this page.

## Read these, in this order

| | Document | Read it when |
|---|---|---|
| 0 | [../vision/000-goal.md](../vision/000-goal.md) | to understand what this system is for |
| 1 | **[10-golden-rules.md](10-golden-rules.md)** | **always, before touching anything** |
| 2 | [20-trading-safety.md](20-trading-safety.md) | before any change near orders, sizing or the bridge |
| 3 | [30-architecture.md](30-architecture.md) | before adding a file or an import |
| 4 | [40-testing.md](40-testing.md) | before writing a test — i.e. before writing code |
| 5 | [50-workflow.md](50-workflow.md) | how a change gets made, end to end |
| 6 | [60-adding-a-tunable.md](60-adding-a-tunable.md) | when a constant should be user-editable |
| 7 | [70-file-organisation.md](70-file-organisation.md) | when a file is too big |

These are plain Markdown on purpose. Any agent — Claude, Cursor, Copilot, a
human with an editor — reads the same rules. Nothing here depends on a
particular tool.

## The thirty-second version

- **Never** place, close or modify a real or demo order to test something.
- **Never** edit a test to make a change pass.
- Write the test first, and **watch it fail** before you make it pass.
- Run the full suite and all four gates before committing.
- Report what you actually did, including what you skipped.

## Why the rules are this strict

An earlier refactor of this codebase was declared complete. It was not. An
audit found ~3,000 lines of extracted code that nothing called, duplicate
implementations that had silently diverged, and — most instructive — a
"guardrail" script that scanned a directory which no longer existed and
printed *"every method delegates correctly"* on every run, for months.

Nobody had ever watched it fail.

So: green output is not evidence. A test that has never been red, a checker
with no negative control, and a confident summary are all the same thing —
comfort without information. The rules here exist to make the difference
visible.

## If you are unsure

Stop and ask. Specifically stop if:

- the change touches order placement, closing or sizing
- a test would need modifying to pass
- a ratchet baseline would need raising
- verifying it properly needs a broker connection

Asking costs one message. The alternative has cost real money here before.

## Questions you cannot answer go in `docs/simon-handover/`

The person running the sessions (Darren) is refactoring this for his brother **Simon**, who holds
the live account, the credentials and the licence, and who makes every trading, risk and money
decision. **Darren usually cannot answer those questions — Simon does.**

So when you hit a decision that isn't yours to make from the code, the rules or a safe default —
especially anything about trading policy, risk numbers, money-path behaviour or licensing — **do not
block and do not silently guess.** Choose a safe provisional default (one that keeps trading no more
aggressive than today), proceed, and record the open decision as a file in
[../../simon-handover/](../../simon-handover/) (see its README for the format). Simon reviews that queue and
answers it. A provisional default is never silent: write down what you chose and what changes if the
answer differs. Confirming a queue item is a *decision* — it is **not** the sign-off + demo session
that order/close/sizing changes still require.

New agent or developer picking this up cold: start from **[../../todo/refactor/HANDOFF.md](../../todo/refactor/HANDOFF.md)**.

## Where everything lives

```
docs/system/          the knowledge base — the single point of truth
docs/system/vision/   why the system exists, what success looks like
docs/system/rules/    these rules — agent-agnostic
docs/system/domains/  one living directory per part of the system
docs/specs/           what we are building and why (spec-driven)
docs/todo/            multi-session work packs in progress
docs/todo/refactor/stage0/         audit trail of past work — read-only
```

The domain files under `docs/system/domains/` hold each part's constraints,
known behaviours and open questions. Read the affected domain's README
before designing a change; update it when a change teaches you something.
