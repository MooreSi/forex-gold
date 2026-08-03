# Start here

You are working on a **live FOREX trading application**. It connects to a real
MetaTrader 5 account and places real orders with real money.

That single fact drives everything else on this page.

## Read these, in this order

| | Document | Read it when |
|---|---|---|
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

## Where everything lives

```
docs/ai/            these rules — agent-agnostic
docs/specs/         what we are building and why (spec-driven)
docs/architecture/  how the system is put together
docs/operations/    running it, releasing it, recovering it
docs/decisions/     decisions taken and the reasoning
docs/history/       audit trail of past work — read-only
```
