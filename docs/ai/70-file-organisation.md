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

- **`mt5_bridge.py`** stays a single root-level file. It runs under a
  *different Python interpreter* (Wine/Windows) as a subprocess. Its import
  path is a filesystem path passed on a command line, and the test suite here
  cannot exercise that interpreter. Permanent exemption.
- **Anything untested.** See below — this is the rule that binds hardest.

---

## The precondition: do not split untested code

**A split is only safe when tests can tell you it worked.**

`backend/src/controllers/remote/` is 2,116 lines of licence-token issuance,
revocation and admin-machine authority — with **zero** tests. Splitting it for
a line-count target would be surgery on the auth path with no way to detect a
mistake. The line ceiling is a code-health heuristic; it does not outrank
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

---

## Current status

| File | Lines | Plan |
|---|---|---|
| `frontend/pages/trading.py` | 3,254 | package split |
| `frontend/pages/settings.py` | 3,193 | package split |
| `frontend/app.py` | 1,635 | package split |
| `frontend/pages/history.py` | 1,416 | package split |
| `mt5_bridge.py` | 1,335 | **permanent exemption** — separate interpreter |
| `backend/src/runtime.py` | 1,310 | at its floor by design — see `30-architecture.md` |
| `frontend/pages/test_panel.py` | 1,263 | package split |
| `backend/src/db/database.py` | 1,251 | package split |
| `frontend/pages/ai_trade_analysis.py` | 1,250 | package split |
| `controllers/remote/server.py` | 1,196 | **blocked: needs tests first** |
| `controllers/sync/server.py` | 1,073 | blocked behind remote/ tests |
| `frontend/pages/breakout_panel.py` | 928 | package split |
| `controllers/remote/client.py` | 920 | **blocked: needs tests first** |
| `controllers/sync/client.py` | 867 | blocked behind remote/ tests |
| `frontend/pages/chart.py` | 842 | package split |
| `frontend/pages/reversal_panel.py` | 812 | package split |

The LOC gate is shrink-only: `structure_gates --check` fails if the count of
oversized files rises, or any listed file grows.
