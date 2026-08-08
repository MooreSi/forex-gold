# Architecture — the shape and the enforced boundaries

## The layers

```
run.py                     launcher: logging, licence, then starts the UI
  │
frontend/                  NiceGUI only. Pages, widgets, formatting.
  │                        Knows nothing about SQL or brokers.
  ▼
backend/src/controllers/   Flat modules, one per page: <name>_controller.py.
  │                        Validate early, call one service, return plain
  │                        values. No loops, merges, formatting or fallbacks.
  ▼
backend/src/services/      All the behaviour. One package per domain.
  │                        Each owns its own repo/*.py for data access, and
  │                        its own off-loop dispatch (to_db_thread).
  ▼
backend/src/db/            Connections, schema, transactions.

backend/src/utils/         Bottom of the stack. Imports nothing above it.
backend/src/config/        Same.

backend/src/runtime.py     TradingRuntime: task supervisor + facade.
backend/src/app.py         Composition root: builds and wires everything.
mt5_bridge.py              Separate process, different Python interpreter.
```

## What each layer may not do

| Layer | Must never |
|---|---|
| `frontend/` | import `backend.src.db`, run SQL, block the event loop |
| `controllers/` | import `backend.src.db`, import a service's `repo`, hold logic |
| `services/` | import `nicegui`, import a controller |
| `utils/`, `config/` | import anything from `backend.src` above themselves |

These are checked, by name, on every test run:

```bash
python -m tools.refactor_audit.import_contracts --check
```

Four contracts are enforced at **zero** — `controllers-never-import-repos`,
`controllers-never-import-the-database`, `services-never-import-controllers`
and `frontend-never-imports-the-database`. Any violation fails. Three carry a
shrink-only baseline because they still have known violations; the number may
go down, never up.

## Controllers route; services decide

A controller names the operations a page can perform and forwards each to one
service. That is the whole job. If you find yourself writing a loop, merging
two sources, formatting a timestamp or catching an exception to return a
default, it belongs in a service.

Two rules exist because breaking them is how the layer collapsed before:

**Controllers do not import `backend.src.db`.** A controller that can reach
the database does not need a service to exist, so none gets written and the
logic pools in the controller. `history/controller.py` reached 403 lines of
three-source ledger merges this way, under a docstring claiming "nothing
touches the database". `db/database.py` also re-exports ~90 names *upward*
from services, so `db_module.get_risk_settings()` reached a service repo
without ever naming it — invisible to the never-import-repos contract.

**Services own off-loop dispatch.** Any service function a `ui.timer` reaches
has an `async` sibling that wraps `to_db_thread`; the controller just awaits
it. This is a change from the original design, where controllers owned
`to_db_thread` — that ownership was precisely what forced every controller to
import `backend.src.db`, and it is why the boundary was unenforceable.

There is no generic dispatch hatch. `run_db(fn)` used to let a page hand an
arbitrary callable through the controller onto the DB worker thread, which
inverted the layering: the *page* chose the data access and the controller
only supplied a thread. Every one of its 51 call sites is now a named service
function.

### Why `controllers/` is flat

`<name>_controller.py` modules, no package directories. This is a deliberate
exception to [70-file-organisation.md](70-file-organisation.md), whose
"package directories, not sibling files" rule exists for modules over 800
lines. A controller that approaches 800 lines has already failed the rule
above, so the exception is safe — and it is enforced rather than trusted:

```bash
python -m tools.refactor_audit.structure_gates --check
```

`controller_loc` (ceiling 200) and `controller_shape` (flat `*_controller.py`
only) are both enforced at zero. The shape gate exists because a package
directory under `controllers/` is exactly how `remote/` and `sync/` grew to
4,950 lines of websocket server, TLS setup and licence issuance without
anyone noticing they were not controllers at all. They are now
`services/cluster/remote/` and `services/cluster/sync/`.

## Where a helper lives

- Used by **two or more services** → `backend/src/utils/`
- Used by **one service** → inside that service's package
- Used only by the **UI** → `frontend/`

Applying this moved three modules out of `utils/`: `retention.py` (only
`db/database.py` used it, and it imports `db()`) to `backend/src/db/`,
`self_healer.py` (only the runtime used it, and it imported the runtime back)
to `services/health/`, and `theme.py` to `frontend/`. That last one is worth
noting — `theme.py` is a stylesheet, and the only reason it had ever been
filed as backend was two lines that read and wrote `app_config` directly.
Those now go through `settings_controller`.

## Why the frontend must not touch the database

A NiceGUI callback runs on the event loop. A synchronous SQLite call inside
one blocks every other user interaction and every background task for its
duration — this produced measured 400–600ms UI stalls. Controllers dispatch DB
work to a worker thread (`db_module.to_db_thread`) so the loop stays free.

That is why the rule is absolute rather than "prefer not to".

## TradingRuntime

`backend/src/runtime.py` is deliberately **not** dissolved into free
functions. It is:

- **the task supervisor** — owns 13 asyncio task handles and their lifetimes
- **the state holder** — mutable caches shared across cycles
- **a curated facade** — ~39 intentional public methods that bind the bridge
  and caches in one place
- **context builders** — `_make_close_trade_ctx`, `_make_scan_ctx`,
  `_make_monitor_ctx`, `_make_position_sync_ctx`, `_make_bot_deps`

Everything else lives in a service. A loop shell on the runtime owns the task;
the service owns what the task does.

**Why a facade and not full dissolution:** dissolving it meant ~90 call sites
each hand-carrying eight collaborators. Dropping one collaborator at one call
site is invisible in review and produces exactly the dead, half-wired code an
audit found here before. One binding site is the safer trade.

`facade_audit` enforces this: the method count may only shrink, and every
public method must be in `facade_allowlist.json`.

```bash
python -m tools.refactor_audit.facade_audit --check
```

## The context-object pattern

When a body moves out of the runtime, its `self.X` references become fields on
a context dataclass built by the runtime. Two rules learned the hard way:

**Share mutable state by reference, never copy it.** The monitor cycle's
counters exist to count cycles — a per-call copy resets them, and MT5
reconciliation, the profit sweep and DPM calibration silently never fire
again. Same for the broker-close miss streak, which counts *consecutive*
misses.

**Read live values through callables, not captured bools.** `is_running` is a
callable because `shutdown()` flips the flag while the loop is awaiting. A
captured `True` keeps the loop spinning after the app was told to stop.

## The two databases

- **The trading database** — trades, signals, risk settings. Uses
  `db()`/`transaction()`, which nest: the outermost block is the commit
  boundary.
- **Per-engine research databases** (reversal engine, test signal) — use their
  own `_conn()`, which does **not** nest. Each call opens a fresh connection.
  Multi-step writes there are not atomic; this is a known gap.

## Where the rules are enforced

| Check | Command |
|---|---|
| Layering | `python -m tools.refactor_audit.import_contracts --check` |
| Structure (LOC, SQL, UI-DB, transactions) | `python -m tools.refactor_audit.structure_gates --check` |
| Runtime facade | `python -m tools.refactor_audit.facade_audit --check` |
| Dead extractions | `python -m tools.refactor_audit.orphan_detector` |

All four also run as tests, so `pytest tests/refactor/` covers them.
