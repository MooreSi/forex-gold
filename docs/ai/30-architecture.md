# Architecture — the shape and the enforced boundaries

## The layers

```
run.py                     launcher: logging, licence, then starts the UI
  │
frontend/                  NiceGUI only. Pages, widgets, formatting.
  │                        Knows nothing about SQL or brokers.
  ▼
backend/src/controllers/   Translation layer. UI shapes ⇄ service calls.
  │                        Owns off-loop dispatch (to_db_thread).
  ▼
backend/src/services/      All the behaviour. One package per domain.
  │                        Each owns its own repo/*.py for data access.
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
| `controllers/` | import a service's `repo` module directly |
| `services/` | import `nicegui` |
| `utils/`, `config/` | import anything from `backend.src` above themselves |

These are checked, by name, on every test run:

```bash
python -m tools.refactor_audit.import_contracts --check
```

Two contracts are enforced at **zero** — `controllers-never-import-repos`
and `frontend-never-imports-the-database`. Any violation fails. Three carry a
shrink-only baseline because they still have known violations; the number may
go down, never up.

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
