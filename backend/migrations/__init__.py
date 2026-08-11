"""Database schema: base DDL, the numbered migration registry, and backfills.

Moved out of backend/src/db/ (2026-08-11, Darren's call) so schema
*evolution* lives apart from the runtime data-access layer:

- schema_sql.py  — the base CREATE TABLE DDL. Deliberately WITHOUT the
  migration-added columns, so executing it alone reproduces the
  pre-migration legacy shape (the legacy-upgrade tests rely on this).
- registry.py    — the ordered, numbered migration steps + run().
  Append-only; never renumber or edit an existing step.
- backfills.py   — named every-boot data corrections, fail-loud.

Alembic was considered and rejected (2026-08-11): there is no SQLAlchemy
to autogenerate against, it would be a new installer dependency, and the
registry already provides the properties that matter — ordered, versioned
per step, fail-closed, legacy-shape-tested. See
docs/system/domains/data/README.md.

The runtime entry point is db/database.py:_apply_schema(), which executes
SCHEMA then calls run() and backfills. This package is data-layer for the
SQL structure gate (tools/refactor_audit/structure_gates.is_repo_file).
"""
from backend.migrations.registry import (  # noqa: F401
    CRITICAL_SCHEMA,
    MIGRATIONS,
    SCHEMA_VERSION,
    apply_migration,
    get_schema_version,
    run,
    stamp_schema_version,
    verify_critical_schema,
)
from backend.migrations.schema_sql import SCHEMA  # noqa: F401
from backend.migrations import backfills  # noqa: F401
