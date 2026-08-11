# Data

**Living file — update when this domain teaches you something.**
Covers: `backend/src/db/`, plus the `app_config` service in
`services/risk/app_config.py`.

## What it is

A single SQLite file per account environment holding both the
trading-engine and Telegram-reader schema, accessed through a shared
repo/adapter layer. `db/database.py` owns the connection cache, the
transaction contextmanager, the full schema and a large re-export surface;
`db/adapter.py` + `sqlite_adapter.py` + `connection.py` are the newer typed,
namespaced adapter used by the per-engine repos. User preferences live in a
plain `app_config` key/value table read and written through a service, never
directly. Note the research engines have their **own separate database
files** — not everything is in the unified DB.

## Where the code lives

- `db/__init__.py` — exposes `transaction` (alias for `database.db`) so multi-write repos can declare their transaction boundary
- `db/database.py` — unified SQLite layer: `init()`, `db()`, `to_db_thread()`, `row_to_dict()`, cache-invalidator registry, split-module re-exports
- `backend/migrations/` — schema evolution, kept apart from the runtime data-access layer (moved out of `db/` 2026-08-11; it counts as data layer for the SQL structure gate):
  - `schema_sql.py` — the base DDL (`SCHEMA`); deliberately does NOT include migration-added columns, so executing it alone reproduces the pre-migration legacy shape (the legacy-upgrade tests rely on this)
  - `registry.py` — the ordered, numbered migration registry (`MIGRATIONS`, `run()`): 12 steps as of 2026-08-11, per-step `schema_version` stamp, fail-closed (`apply_migration` skips duplicate-column/already-exists, aborts on anything else). NEVER renumber/reorder/edit an existing step — append.
  - `backfills.py` — named every-boot data backfills (rebrand renames, instant:-prefix strip, order_type, DPM tg_source): idempotent, missing table/column benign, any other failure aborts startup
  - **Alembic considered and rejected (2026-08-11):** no SQLAlchemy metadata to autogenerate against (the DB layer is raw sqlite3), it would be a new Windows-installer runtime dependency, and the registry already gives the properties that matter — ordered, per-step versioned, fail-closed, legacy-shape-tested. Revisit only if SQLAlchemy ever arrives.
- `db/adapter.py` — the `DbAdapter` Protocol and `RunResult`; repos depend on this, never on `sqlite3`
- `db/sqlite_adapter.py` — the SQLite implementation (`Row` factory, foreign keys ON, WAL)
- `db/connection.py` — namespaced module-level adapter registry, one connection slot per engine
- `db/retention.py` / `retention_repo.py` — data-retention pruning; one `db()` block prunes all tables in one commit
- `services/risk/app_config.py` — the `app_config` key/value service over `app_config_repo`

## Constraints / must not change

- **No raw `sqlite3` outside the `db` package** — repos go through the adapter. Enforced by the structure gate.
- The frontend never touches the DB; controllers never import `backend.src.db` or a service's repo. Both enforced at zero.
- `app_config` is read/written through the service, never the repo directly — the repo is where SQL and cache invalidation live.
- `database.db()` at depth 1 is the transaction boundary; the outermost block commits or rolls back everything inside it. The `db.transaction` alias exists so the structure gate can verify multi-write repos declare it.
- The `*_repo.py` split modules are **verbatim ports** of the old `core/database.py` — same functions, same SQL — re-exported so every `db_module.<name>` call site works unchanged.
- Retention table names come from a fixed literal tuple, never user input (the f-string DELETE depends on that).

## Known things & gotchas

- `db()` caches one connection **per thread**. `init()` explicitly closes the calling thread's and the DB worker's cached connections before re-pointing `_DB_PATH` — without that, a live/demo switch left writes going to the OLD file (found 2026-07-21).
- `init()` also resets the 10s risk-settings memo — otherwise the app answered with the *other* environment's session gates for ten seconds after a switch.
- Any DB-derived cache must register via `register_cache_invalidator()`; a broken invalidator is logged and must not block the environment switch.
- `db()` is re-entrant via a per-thread depth counter — an inner block joins the outer transaction, never commits out from under it.
- `connection.py` is namespaced because a single bare `_adapter` global was a real bug: each engine's `init_db()` overwrote it, so all but the last engine silently queried the wrong file (confirmed 2026-07-21).
- `database.py` has a `__getattr__` lazy shim for a handful of analytics names to avoid an import cycle.
- Live strategy parameters live in `app_config` under `strategy_params_{strategy}`; `strategy_param_templates` is only the saved library.
- `DB_PATH` in `config/__init__.py` is a pre-`load()` fallback only; the real path resolves from `account_env`.
- `schema_version.version` = the **last applied migration step number** (2026-08-11; previously a constant 1). A DB stamped at N resumes from N+1; all steps stay idempotent so re-running old steps is safe but the stamp is the record. Backfills are NOT versioned — they run every boot on purpose (a legacy-shaped row can arrive later via restore/sync).

## Open questions

- None currently flagged. (Cross-engine database consolidation is tracked under the engines domain.)
