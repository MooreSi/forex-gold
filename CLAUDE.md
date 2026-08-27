# CLAUDE.md

**This app places real orders on a live MetaTrader 5 account with real money.**

Read **[docs/system/rules/10-golden-rules.md](docs/system/rules/10-golden-rules.md)** before
changing anything. It is short and it is not optional.

**New here / picking this up cold?** Start at **[docs/todo/refactor/HANDOFF.md](docs/todo/refactor/HANDOFF.md)** — who's who, how to run it
locally, current state, and where the work is tracked.

**A question you can't answer** (trading policy, risk numbers, money behaviour, licensing) goes in
**[docs/simon-handover/](docs/simon-handover/)** — the owner's brother Simon answers those; the person running
the sessions usually can't. Choose a safe provisional default, proceed, and record the open decision.

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

## Session mechanics (Windows) — hard-won, do not relearn

Each of these cost real time in a past session:

- **Never edit tracked files while `tools.checks all` (or the suite) is
  running.** Mid-run edits produce phantom gate failures and wasted 8-minute
  runs. Docs-only edits are the one exception.
- **Never string-edit source files through PowerShell** (`Get-Content |
  .Replace() | Set-Content` mangles UTF-8 to mojibake). Use the file tools
  or Python.
- **Commit with `git commit -F <msgfile>`** — multiline `-m` here-strings
  break under PowerShell 5.1.
- **Start every shell command from an absolute path** — Bash cwd persists
  across calls and has drifted mid-session before.
- **Before adding lines to a file in `structure_baseline.json`**, check the
  LOC ratchet — baselined files are shrink-only; plan the offsetting shrink
  first or put the code in a new module.
- **A new module nothing imports yet** must ship with its
  `orphan_module_allowlist.json` entry (with reason) in the same change, or
  the orphan gate fails the next full run.
- **`backend.src.config` imports from frontend COUNT against the
  controller-boundary contract** — existing sites are baselined, new ones
  regress it. Inject config values from `frontend/app.py` (already a
  baselined site) instead.
- **Repo-wide scripts must exclude** `.git`, `.venv`, `__pycache__`,
  `docs/todo/refactor/stage0/` (audit trail) and `docs/reviews/`
  (point-in-time snapshots).
- **PS 5.1 `;` chains continue past failures** (no `&&`) — verify state
  after multi-step git chains.
- Check doc links after moving files: `python tools/check_doc_links.py`.
- **After restoring a mutated source file, delete `__pycache__`.** Python
  invalidates bytecode on mtime + size. A mutation that swaps two things of
  the same length (`(sl, tp, id)` -> `(tp, sl, id)`) restored with `cp` in the
  same second leaves BOTH unchanged, so the interpreter reuses the *mutated*
  `.pyc`. This reports the mutant as survived and then runs the rest of the
  session against code that is not on disk. Cost: one wrong "this test is
  vacuous" conclusion, found only because a later test failed in a way the
  source could not explain.
  ```bash
  find . -name '__pycache__' -type d -not -path './.venv/*' -exec rm -rf {} +
  ```
  Note the asymmetry: a stale `.pyc` can only turn a KILLED mutant into a
  survivor, never the reverse. A mutant that failed a test is always a real
  result; a mutant that "survived" a same-length edit is not.

## Do not

- `git push --force` to a shared branch
- commit secrets, tokens or licence keys
- add a licence or auth bypass, even "for testing"
- lower a ratchet baseline to get CI green
- run two full test suites at once (produces phantom failures)
- `pip install` a new runtime dependency without asking
- edit anything under `docs/todo/refactor/stage0/` — it is an audit trail
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
