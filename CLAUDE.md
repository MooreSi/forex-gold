# CLAUDE.md

**This app places real orders on a live MetaTrader 5 account with real money.**

Read **[docs/ai/10-golden-rules.md](docs/ai/10-golden-rules.md)** before
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

## Where the rules live

| Topic | File |
|---|---|
| Start here | [docs/ai/00-start-here.md](docs/ai/00-start-here.md) |
| **Golden rules** | [docs/ai/10-golden-rules.md](docs/ai/10-golden-rules.md) |
| What can cost money | [docs/ai/20-trading-safety.md](docs/ai/20-trading-safety.md) |
| Layers and boundaries | [docs/ai/30-architecture.md](docs/ai/30-architecture.md) |
| Testing protocol | [docs/ai/40-testing.md](docs/ai/40-testing.md) |
| How to make a change | [docs/ai/50-workflow.md](docs/ai/50-workflow.md) |
| Making a constant configurable | [docs/ai/60-adding-a-tunable.md](docs/ai/60-adding-a-tunable.md) |
| Splitting a big file | [docs/ai/70-file-organisation.md](docs/ai/70-file-organisation.md) |

These live in `docs/` as plain Markdown so any tool reads them — not just
Claude Code.

## Skills

| Skill | Use when |
|---|---|
| `/verify` | before every commit — full suite + gates + boot |
| `/safe-change` | any change near orders, sizing or the close path |
| `/add-tunable` | a hardcoded constant should be user-editable |
| `/split-file` | a file is over 800 lines |
| `/new-spec` | starting anything bigger than a one-line fix |
| `/coverage-gap` | find and fill untested code |

## Layers point downward, never up

```
frontend/ → controllers/ → services/ → db/
                utils/, config/ → nothing
```

The frontend never imports `backend.src.db`. Controllers never import a
service's `repo`. Enforced at zero — see
[docs/ai/30-architecture.md](docs/ai/30-architecture.md).

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
