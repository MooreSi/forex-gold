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

- **The migration registry's history and rules (moved here from `backend/migrations/registry.py` 2026-09-09).** That file sits against the 800-line ceiling and every new migration pushed it over — twice in one day — so the prose moved and the rules stayed. **A new migration is ~6 lines; budget for it, or shrink something first.** The original rationale:

```
History: the boot-time migrations were one flat list of ~90 idempotent
ALTER/CREATE statements inside database._apply_schema, originally wrapped in
a single `except Exception: pass` (so a genuinely failed migration was
indistinguishable from an already-applied one), later fail-closed via
apply_migration, and now numbered here so a database records *which*
migrations it has (`schema_version.version` = the last applied step).

Rules for changing this file:
- NEVER renumber, reorder, or edit an existing step — append a new one.
- Every SQL step must stay idempotent (ADD COLUMN / CREATE TABLE IF NOT
  EXISTS); apply_migration skips the benign already-applied errors and
  aborts on anything else.
- The statements below are the verbatim transcription of the old flat loop
  (2026-08-11); the grouping follows the feature waves the comments named.

These functions take a connection so they carry no import dependency on
database.py; database.py calls run() from _apply_schema after the base
CREATE TABLE pass.
```
- `db()` caches one connection **per thread**. `init()` explicitly closes the calling thread's and the DB worker's cached connections before re-pointing `_DB_PATH` — without that, a live/demo switch left writes going to the OLD file (found 2026-07-21).
- `init()` also resets the 10s risk-settings memo — otherwise the app answered with the *other* environment's session gates for ten seconds after a switch.
- Any DB-derived cache must register via `register_cache_invalidator()`; a broken invalidator is logged and must not block the environment switch.
- `db()` is re-entrant via a per-thread depth counter — an inner block joins the outer transaction, never commits out from under it.
- `connection.py` is namespaced because a single bare `_adapter` global was a real bug: each engine's `init_db()` overwrote it, so all but the last engine silently queried the wrong file (confirmed 2026-07-21).
- `database.py` has a `__getattr__` lazy shim for a handful of analytics names to avoid an import cycle.
- Live strategy parameters live in `app_config` under `strategy_params_{strategy}`; `strategy_param_templates` is only the saved library.
- `DB_PATH` in `config/__init__.py` is a pre-`load()` fallback only; the real path resolves from `account_env`.
- `schema_version.version` = the **last applied migration step number** (2026-08-11; previously a constant 1). A DB stamped at N resumes from N+1; all steps stay idempotent so re-running old steps is safe but the stamp is the record. Backfills are NOT versioned — they run every boot on purpose (a legacy-shaped row can arrive later via restore/sync).

- **The migration steps live in `backend/migrations/steps.py`, not `registry.py` (2026-09-10).** A pure move: the list had reached 661 of registry.py's 800 lines, and the next migration would have crossed the LOC ceiling — a wall every future migration hits rather than a problem with any one of them. `registry.py` keeps the machinery that applies them (`apply_migration`, `run`, the stamp, the critical-schema check) and re-exports `MIGRATIONS`, so `backend.migrations.registry.MIGRATIONS` and every other import path are unchanged. `_rename_gdc_column` moved with the list because it *is* step 1, and leaving it behind would have made the two modules import each other. Adding a migration now means editing `steps.py`; `registry.py` is 120 lines and should stay that way.

## Open questions

- None currently flagged. (Cross-engine database consolidation is tracked under the engines domain.)

## The default database path is a fallback, not a spare (2026-09-11)

**Deleting `forex_trader_demo.db` makes the app boot onto a brand-new empty
database.** Learned the hard way, on the live demo account.

`account_registry._resolve_file` returns `default_db_path(...)` whenever the
MT5 login is not yet known, and at boot it is not: the app starts, opens a
database, and only then does the EA say which account it is attached to. So
the default path is on the hot path of every single startup, whatever the
registry says. It is not the "old" file, and it is not spare.

What happened: the 25470480 database WAS the default path and was archived
as an old account. The next boot found no file there, created one, migrated
it to head, and ran against it -- no trades, no templates, no channel
bindings, and every live-execution flag at its schema default. It was inert
for the minute it ran (verified: zero trades, zero signals, no broker
positions) purely because those defaults are off, which is the only reason
this is a story about a scare rather than about money.

It also explains an older puzzle. The archived database sat at schema
version 35 while the account in use was at 41: whichever file the app does
NOT open at boot simply never gets migrated, so a second account's database
silently lags and the settings columns its features need are absent. That is
why migrations 36, 37 and 38 were missing from it, and why the switches for
reversal-engine items 040, 050 and 100 could not have been turned on there
even though the code shipped.

**Resolved by consolidating**: there is now one demo database, and the
registry maps the live account to the default filename, so the fallback and
the resolved path are the same file and cannot diverge.

If a second account is ever used again, the thing to fix first is that
migrations run against the file the app resolves to, not the file it happened
to open first.
