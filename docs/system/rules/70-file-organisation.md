# File organisation — the decision, and how to split a big file

## The decision: package directories, not sibling files

When a module outgrows the 800-line ceiling, it becomes **a package directory
in the same location, keeping its import path**:

```
frontend/pages/trading.py          (3,254 lines)
        ↓
frontend/pages/trading/
    __init__.py        `render()` — the only public name. Re-exports nothing else.
    _open_trades.py    one coherent section
    _signals.py
    _manual_order.py
    _panels.py
```

`from frontend.pages.trading import render` keeps working. Every caller,
every test, every route registration is untouched.

### Why this and not `trading_signals.py` alongside `trading.py`

Both split the file. Only one of them survives contact with the next split.

| | package directory | sibling files |
|---|---|---|
| Import path | unchanged | every caller edits |
| Where the pieces live | one obvious place | scattered by alphabet in a flat directory |
| `pages/` after 4 splits | 4 more entries | ~25 more entries |
| Which file is the entry point | `__init__.py`, always | you have to know |
| Moving a piece between sections | internal, invisible | another caller edit |

The flat approach is what produced `core_open_trade.py`,
`core_close_trade.py`, `core_run_tp_ladder.py`, `core_monitor_loop.py`… in
the old `forex_trader/core/` — 60+ files whose relationships you could only
learn by reading all of them. Directories make the relationship structural.

### Where this does NOT apply

- **`backend/src/controllers/`** is flat: `<name>_controller.py` modules, never
  package directories. A controller routes and does not decide, so it never
  reaches a size that needs splitting -- and `structure_gates` enforces that
  with a 200-line ceiling and a shape check, both at zero. Package directories
  here are what let `remote/` and `sync/` grow to 4,950 lines of websocket
  server inside the controller layer. See
  [30-architecture.md](30-architecture.md).
- **`mt5_bridge.py`** stays a single root-level file. It runs under a
  *different Python interpreter* (Wine/Windows) as a subprocess. Its import
  path is a filesystem path passed on a command line, and the test suite here
  cannot exercise that interpreter. Permanent exemption.
- **Anything untested.** See below — this is the rule that binds hardest.

---

## The precondition: do not split untested code

**A split is only safe when tests can tell you it worked.**

`backend/src/services/cluster/remote/` is 2,092 lines of licence-token
issuance, revocation and admin-machine authority — with **zero** tests. (It
lived under `controllers/` until it was moved to the layer it actually belongs
to; the move was a pure `git mv`, and the line counts below are unchanged
because nothing inside it was reshaped.) Splitting it for a line-count target
would be surgery on the auth path with no way to detect a mistake. The line ceiling is a code-health heuristic; it does not outrank
"can we tell if this broke".

Order of operations, always:

1. tests exist and pass
2. then split
3. then confirm the same tests still pass, unmodified

If step 1 is missing, step 1 *is* the task. Say so instead of splitting.

---

## How to split, mechanically

1. **Pick seams by cohesion, not by line count.** A section that shares state
   or is read top-to-bottom stays together. If you cannot name the section in
   three words, it is not a section.

2. **Create the directory, move the file to `__init__.py`.**
   ```bash
   mkdir frontend/pages/trading
   git mv frontend/pages/trading.py frontend/pages/trading/__init__.py
   ```
   Commit this alone. It is a pure move — `git` records it as a rename and the
   next diff is readable.

3. **Move one section per commit**, into `_section.py`. Prefix with `_`: these
   are internals of the package, not a public API.

4. **`__init__.py` keeps only the public name.** For a page, that is
   `render()`. Anything a sibling section needs is imported explicitly, never
   re-exported "just in case".

5. **Watch for module-level state.** If the file rebinds a module global via
   `global`, splitting *forks that state* — each module gets its own copy and
   writes go to the wrong one. Move the state into a small state module both
   sections import, or do not split.

6. **Run the full suite after each commit.** Not at the end.

## What the trading split actually cost

The first real split (`frontend/pages/trading.py`, 3,254 lines → nine
modules) hit four problems worth knowing about in advance. None were about
the code being moved.

**Module-level assignments do not follow their functions.** A section
calling `log.warning(...)` in an exception handler while
`log = logging.getLogger(__name__)` stayed in `__init__.py` imports
cleanly, renders cleanly, and raises `NameError` the first time that error
path runs — replacing the real error with a confusing one.
`tests/frontend/test_page_packages_are_wired.py` now catches this
statically, across branches no test executes.

**Bare references are references.** A dependency scan matching `name(` misses
a function used as a dict value or passed as a callback. The comparison-table
cells were exactly that.

**Circular imports appear from mis-assignment, not from bad design.** Two
helpers landed in `_schedule` while their only caller was `_strategy`, and
`_schedule` already needed something from `_strategy`. The fix is to move the
function to its caller, not to add a lazy import.

**Prune imports last.** Pruning a module and then moving more code into it
re-creates the need for imports you just removed.

**Splitting can move a metric without moving reality.** The import-contract
count rose from 99 to 103 purely because the same imports were spread over
more files. The contract now counts distinct (source unit → module) edges,
with a split page package counting as one unit, so a split is metric-neutral.
That was a flaw in the metric, and it was fixed rather than baselined around.

---

## After any split, check the moved code can still see what it uses

A verbatim move takes the function body and leaves the module-level constants
and imports it reads behind. Nothing fails until that line runs.

```bash
python -m tools.refactor_audit.undefined_names backend frontend tools
```

It is the fifth check in `python -m tools.checks all`, so a full run covers it.
Four splits in this repo have shipped this bug —
`docs/todo/bugs/010`, `011` and `018`.

## Current status

Measured 2026-08-31, not remembered. **Three files exceed the 800-line ceiling
in the whole tree, and two of them are permanently exempt.**

| File | Lines | Plan |
|---|---|---|
| `backend/src/runtime.py` | 1,508 | **exempt** — at its floor by design, see `30-architecture.md` |
| `mt5_bridge.py` | 1,344 | **exempt** — runs under a separate interpreter |
| `services/cluster/remote/server.py` | 1,204 | the only real one left. See below. |

Everything else that was ever on this list is done: `frontend/pages/trading.py`
(3,254), `settings.py` (3,487), `app.py` (1,746), `history.py` (1,592),
`test_panel.py` (1,245), `ai_trade_analysis.py` (1,250), `breakout_panel.py`
(918), `telegram.py` (892), `chart.py` (938), `reversal_panel.py` (923) are all
packages now, and `db/database.py` is 457. The four cluster files came down
under the ceiling on 2026-08-29/30 (`sync/server` 1,085 → 732, `remote/client`
894 → 732, `sync/client` 867 → 709).

### The one that is left

`services/cluster/remote/server.py` is 1,204 lines of token issuance, licence
delivery and admin authority. It was listed for years as "blocked: needs tests
first". That was true when written and is **no longer the reason to leave it
alone**: as of 2026-08-31 it is at 58% coverage, and its connection front door
(`_handler`, the largest block in the file) went from 145 uncovered lines to
65 — see `tests/remote/test_connection_auth.py`.

The reason it stays whole is now a different one, and it is worth stating so
nobody "unblocks" it by writing more tests. Measured 2026-08-31: the module
carries **six `global` rebinding statements** covering `_allowed_tokens`,
`_pending`, `_revoked_tokens`, `_admin_machines`, the six KeyGen callbacks and
the server task/object, and **five test files reach in and patch that state**
(`tests/remote/test_connection_auth.py`, `test_admin_commands.py`,
`test_licence_lifecycle.py`, `test_commit_reporting.py`, and
`tests/core/test_bot_panel_actions.py`).

Splitting a module that rebinds globals forks that state -- each half gets its
own copy and writes land in the wrong one. That is the "check for module-level
state" step of the procedure above, and here it says stop.

Splitting it needs the state moved into one small module both halves import,
as a deliberate change with its own tests, ahead of any file movement. Until
someone does that, the ceiling loses to "can we tell if this broke".

The LOC gate is shrink-only: `structure_gates --check` fails if the count of
oversized files rises, or any listed file grows.

Also blocked on tests: `services/cluster/remote/server.py` and `client.py`. The
auth *decisions* are now covered (`tests/controllers/test_remote_server_auth.py`,
`test_remote_admin_password.py`), but the websocket handler and TLS setup are
not, and those are the bulk of both files.

## The one file that stays over the ceiling, on purpose

`backend/src/services/cluster/remote/server.py` (1,177 after its stateless
halves were extracted, 2026-08-29).

It keeps ten module-level mutable containers and rebinds several with `global`.
Splitting code that touches those forks the state, which is the hazard
`/split-file` names. Moving the state to a shared module is the documented
alternative, and it is not done because of the cost: ~125 reference rewrites,
plus **five test files that patch those names directly to isolate themselves**.

Silently changing where those tests patch risks one of them no longer isolating
anything while still passing -- on the module that issues licences and holds
admin authority. That is the "green output is not evidence" failure this
codebase already had once.

The stateless sections (LAN beacon, version/changelog) moved to
`_beacon_version.py`, which lowers the shrink-only baseline without touching
any of it. Revisit if the state is ever consolidated for its own reasons; do
not do it *in order to* hit the line count.
