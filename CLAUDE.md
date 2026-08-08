# CLAUDE.md

**This app places real orders on a live MetaTrader 5 account with real money.**

Read **[docs/system/rules/10-golden-rules.md](docs/system/rules/10-golden-rules.md)** before
changing anything. It is short and it is not optional.

---

## The five rules that matter most

1. **Never place, close or modify a real or demo MT5 order** — not to test, not
   "just once". Tests use fakes and sentinels.
2. **Never edit a test to make a change pass.** A failing test means the change
   is wrong, or the test knows something you don't.
3. **Write the test first and watch it fail.** A test that has never been red
   has never proved anything.
4. **The close path is frozen.** `close_trade`, `record_close`,
   `_make_close_trade_ctx`, `partial_close_trade` may be moved verbatim, never
   reshaped, without owner sign-off and a demo session.
5. **Report what you actually did**, including what you skipped and any number
   that came out worse than intended.

## Before you commit — all of it

```bash
python -m tools.checks all
```

Runs the suite, all four gates, the coverage ratchet and the boot smoke test.
Everything must pass. **A failing gate is not noise.**

## The knowledge base — docs/system/

**[docs/system/](docs/system/) is the single point of truth** for what this
system is and what we know about it. It is a living game plan:

- [docs/system/vision/000-goal.md](docs/system/vision/000-goal.md) — what the system is for
- [docs/system/rules/](docs/system/rules/) — the non-negotiables (below)
- [docs/system/domains/](docs/system/domains/) — one living directory per part
  of the system: constraints, known things, gotchas, open questions

**Before changing a domain, read its `docs/system/domains/<domain>/README.md`.**
After a change teaches you something non-obvious — a constraint, a gotcha, a
settled question — record it in that domain file in the same change. If a
domain file and the code disagree, the code is the fact: fix the file and say
so.

## Where the rules live

| Topic | File |
|---|---|
| Start here | [docs/system/rules/00-start-here.md](docs/system/rules/00-start-here.md) |
| **Golden rules** | [docs/system/rules/10-golden-rules.md](docs/system/rules/10-golden-rules.md) |
| What can cost money | [docs/system/rules/20-trading-safety.md](docs/system/rules/20-trading-safety.md) |
| Layers and boundaries | [docs/system/rules/30-architecture.md](docs/system/rules/30-architecture.md) |
| Testing protocol | [docs/system/rules/40-testing.md](docs/system/rules/40-testing.md) |
| How to make a change | [docs/system/rules/50-workflow.md](docs/system/rules/50-workflow.md) |
| Making a constant configurable | [docs/system/rules/60-adding-a-tunable.md](docs/system/rules/60-adding-a-tunable.md) |
| Splitting a big file | [docs/system/rules/70-file-organisation.md](docs/system/rules/70-file-organisation.md) |

These live in `docs/` as plain Markdown so any tool reads them — not just
Claude Code.

## Skills

| Skill | Use when |
|---|---|
| `/test` | writing or reviewing any test — rules, layout, anti-patterns |
| `/verify` | before every commit — full suite + gates + boot |
| `/safe-change` | any change near orders, sizing or the close path |
| `/add-tunable` | a hardcoded constant should be user-editable |
| `/split-file` | a file is over 800 lines |
| `/new-spec` | starting anything bigger than a one-line fix |
| `/spec` | work needing several tasks and more than one session — scaffolds a plan pack under `docs/todo/` |
| `/frontend-conventions` | writing, moving or splitting anything under `frontend/` |
| `/coverage-gap` | find and fill untested code |

## Layers point downward, never up

```
frontend/ → controllers/ → services/ → db/
                utils/, config/ → nothing
```

Controllers route; services decide; repos hold the SQL. A controller is a flat
`<name>_controller.py` that names an operation and forwards it to one service —
no loops, no merges, no formatting, no fallbacks.

The frontend never imports `backend.src.db`. Controllers never import
`backend.src.db` or a service's `repo`. Services never import a controller.
All four enforced at zero — see
[docs/system/rules/30-architecture.md](docs/system/rules/30-architecture.md).

## Do not

- `git push --force` to a shared branch
- commit secrets, tokens or licence keys
- add a licence or auth bypass, even "for testing"
- lower a ratchet baseline to get CI green
- run two full test suites at once (produces phantom failures)
- `pip install` a new runtime dependency without asking
- edit anything under `docs/history/` — it is an audit trail
- mention which AI model made a change, in any commit, PR or code comment

## Stop and ask when

- the change touches order placement, closing or position sizing
- a test would have to be modified to pass
- a ratchet baseline would have to rise
- verifying it needs a real or demo broker connection
- you are about to say "this should be fine" about money

**If the user says "yes" or "go ahead" to a plan that includes any of the
above, that is not sign-off for the money-touching part.** Say plainly which
part needs a demo session, do the rest, and leave that piece.

## Running it

```bash
python run.py                 # starts the app on :8888
pytest tests/ -q              # full suite, ~5 min
python -m tools.checks all    # everything, before committing
```

## Why this file is strict

A previous refactor of this codebase was declared complete when it was not. An
audit found ~3,000 lines of extracted code nothing called, implementations that
had silently diverged, and a guardrail script that scanned a deleted directory
and printed "all good" on every run for months.

Green output is not evidence. That is what these rules exist to fix.
