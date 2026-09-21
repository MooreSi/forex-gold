# CLAUDE.md

**This app places real orders on a live MetaTrader 5 account with real money.**

Read **[docs/system/rules/10-golden-rules.md](docs/system/rules/10-golden-rules.md)** before
changing anything. It is short and it is not optional.

**New here / picking this up cold?** Start at **[docs/todo/refactor/HANDOFF.md](docs/todo/refactor/HANDOFF.md)** — who's who, how to run it
locally, current state, and where the work is tracked.

**A question you can't answer** (trading policy, risk numbers, money behaviour, licensing) goes in
**[docs/simon-handover/](docs/simon-handover/)** — the owner answers those; an agent can't. Choose a safe
provisional default, proceed, and record the open decision.

---

## The five rules that matter most

1. **Never place, close or modify a real trade on a live account.** Testing on
   the **demo** account is allowed (owner, 2026-09-09). The automated suite
   still touches no broker at all — it uses fakes and sentinels, because it
   runs unattended on CI and on other people's machines.
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
.venv/bin/python -m tools.checks all
```

Runs the suite, all four gates, the coverage ratchet and the boot smoke test.
Everything must pass. **A failing gate is not noise.**

**Use `.venv/bin/python`, never bare `python`** — see Session mechanics.

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
| **Two checkouts, one data dir** | [docs/system/rules/80-two-checkouts-one-data-dir.md](docs/system/rules/80-two-checkouts-one-data-dir.md) |

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
| `/frontend-conventions` | writing, moving or splitting anything under `frontend/src/` or `backend/src/api/` |
| `/coverage-gap` | find and fill untested code |

## Layers point downward, never up

```
frontend/ (React, in the browser)
    │ HTTP/JSON
backend/src/api/ → controllers/ → services/ → db/
                       utils/, config/ → nothing
```

Routers forward; controllers route; services decide; repos hold the SQL. A
controller is a flat `<name>_controller.py` that names an operation and
forwards it to one service — no loops, no merges, no formatting, no fallbacks.
A router is one controller call plus a response model, held to the same rule
and the same 200-line ceiling.

`backend/src/api/` never imports `backend.src.db` or `backend.src.services`.
Controllers never import `backend.src.db` or a service's `repo`. Services never
import a controller. All enforced at zero — see
[docs/system/rules/30-architecture.md](docs/system/rules/30-architecture.md).
`backend/src/api/server.py` is the single named exemption: it is the
composition root and holds the engine handle.

**The dashboard is React** (`frontend/src`, compiled to `frontend/dist`, which
is committed). It replaced NiceGUI on 2026-09-18 — the decision and the
2026-08-06 one it reverses are in
[docs/system/domains/frontend/010-the-react-decision.md](docs/system/domains/frontend/010-the-react-decision.md),
and what is and is not ported is in
[docs/todo/frontend/react-port/](docs/todo/frontend/react-port/README.md).

## Session mechanics (Windows) — hard-won, do not relearn

Each of these cost real time in a past session:

- **Run everything through `.venv/bin/python`, never bare `python`.** The
  project's environment is `.venv` (3.13 — the `__pycache__` files say
  `cpython-313`). Bare `python` on the owner's Mac is a pyenv 3.11 with a
  different, incomplete package set, and on Windows it is whatever is on
  PATH. Getting this wrong does not fail loudly: on 2026-09-21 a full
  session ran under 3.11 and reported **20 fabricated "pre-existing"
  failures** to the owner as fact — the 15 reversal-engine ML tests, 4
  news-calendar tests, and the whole of `tests/api` as uncollectable
  (`ModuleNotFoundError: itsdangerous`, a dependency `requirements.txt`
  declares and that venv simply lacked). All 20 pass. They were reported
  twice, the second time "confirmed" by reproducing them in a throwaway
  worktree — which shared the same wrong interpreter, so the confirmation
  confirmed nothing.
  **A missing third-party module is almost never a real finding here. It is
  this.** Check `head -1 $(which python)` against `.venv/bin/python` before
  concluding anything about a failure you did not cause.
- **`git archive` is not a checkout.** Verifying a commit in isolation needs
  `git worktree add --detach <dir> <rev>`, outside the repo. An archive has
  no `.git`, so every test that shells out to git — `.gitattributes` line
  endings, whether `.gitignore` covers a path, `test_bundle_is_reproducible`,
  `test_bundled_ca` — fails for a reason that has nothing to do with the
  change. Same session, 7 more false failures.
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
- **A `frontend/src` change that is not rebuilt is not shipped.** `dist/` is
  committed; run `npm run build` in the same change or the dashboard the user
  sees is the previous one.
- **Before adding lines to a file in `structure_baseline.json`**, check the
  LOC ratchet — baselined files are shrink-only; plan the offsetting shrink
  first or put the code in a new module.
- **A new module nothing imports yet** must ship with its
  `orphan_module_allowlist.json` entry (with reason) in the same change, or
  the orphan gate fails the next full run.
- **`backend.src.config` imports from `backend/src/api/` COUNT against the
  controller-boundary contract**, which is now enforced at zero with no
  baseline at all. Get config values through `settings_controller`, or inject
  them from `backend/src/api/server.py` — the one exempt site.
- **A test fixture that opens a database must close it before `os.remove`.**
  POSIX lets you unlink a file that still has an open handle; Windows does
  not, and raises `PermissionError: [WinError 32] The process cannot access
  the file because it is being used by another process`. The first Windows
  CI run this repo ever completed (2026-08-27) produced **50 teardown errors**
  from exactly this, in fixtures that had passed on macOS since the day they
  were written. Use `re_repo.close_db()` for an engine repo, or
  `reset_thread_local_connection()` + `reset_db_worker_thread_connection()`
  for the shared `db` module — `db.init()` leaves a handle on the calling
  thread AND on the `to_db_thread` worker. `tests/conftest.py`'s `fresh_db`
  is the reference; local copies of it are where this keeps going wrong.
- **The suite is ~7x slower on Windows CI than on macOS** — 28m48s against
  239s, for the same 3,623 tests. Budget for it: the workflow's
  `timeout-minutes` is 60, and every push to `main` costs a full run.
- **Repo-wide scripts must exclude** `.git`, `.venv`, `__pycache__`,
  `.claude/` (agent worktrees), `docs/todo/refactor/stage0/` (audit trail)
  and `docs/reviews/` (point-in-time snapshots).
- **`.claude/worktrees/<name>/` is a COMPLETE second checkout.** Spawning one
  background task took four gates from green to red with ~370 duplicated
  files: the LOC gate reported the worktree's `runtime.py` as a new
  violation, the orphan gate reported ~300 unrecorded orphans, and the
  duplicate-implementation detector found every function twice. None of it
  was real. Both scanner `EXCLUDED_DIRS` sets now carry `.claude`; if you add
  a third scanner, it needs the same. Pinned by
  `tests/refactor/test_scanners_ignore_worktrees.py`.
- **PS 5.1 `;` chains continue past failures** (no `&&`) — verify state
  after multi-step git chains.
- Check doc links after moving files: `python tools/check_doc_links.py`.
- **`~/Forex-Update` and `~/Forex-React` share one `USER_DATA_DIR`** — one
  `config.yaml`, one `forex_trader_<env>.db`, one bridge port. That is
  deliberate: it is what lets the owner switch between the two apps. Only one
  may RUN at a time (`utils/single_instance.py`, claimed in `run.main()`
  before anything opens the database). **The version number is the exception:
  it is per-checkout and must never be stored in the shared data directory or
  a database.** Both rules, and why, in
  [docs/system/rules/80-two-checkouts-one-data-dir.md](docs/system/rules/80-two-checkouts-one-data-dir.md).
  The lock module and its call site are identical in both checkouts; change
  them together.
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
- verifying it needs a LIVE broker connection (demo is yours to use)
- you are about to say "this should be fine" about money

**If the user says "yes" or "go ahead" to a plan that includes any of the
above, that is not sign-off for the money-touching part.** Say plainly which
part needs a demo session, do the rest, and leave that piece.

## Running it

Every Python command goes through `.venv/bin/python`. Bare `python` is a
different interpreter with a different package set — see Session mechanics
for what that cost.

```bash
.venv/bin/python run.py                 # starts the app on :8888
.venv/bin/python -m pytest tests/ -q    # full suite, ~7.5 min on this Mac
.venv/bin/python -m tools.checks all    # everything, before committing

cd frontend && npm install    # once, per checkout
cd frontend && npm test       # the dashboard's own suite (vitest)
cd frontend && npm run build  # rebuild dist/ — commit it with your src change
```

**`frontend/dist` is committed and is what the app serves.** A change under
`frontend/src` that does not rebuild it ships the previous dashboard. Node is a
developer dependency only; nothing about the install changes for a user.

## Why this file is strict

A previous refactor of this codebase was declared complete when it was not. An
audit found ~3,000 lines of extracted code nothing called, implementations that
had silently diverged, and a guardrail script that scanned a deleted directory
and printed "all good" on every run for months.

Green output is not evidence. That is what these rules exist to fix.
