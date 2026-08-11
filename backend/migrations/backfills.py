"""Named data backfills — one-off row corrections, applied on every boot.

These are the data-migration siblings of db/migrations.py's schema steps:
historical corrections (the 2026-07-23 GD-Copy-Engine -> Reversal-Engine
rebrand, the legacy "instant:" prefix, the pre-column order_type default)
that must also catch any legacy-shaped row that arrives later (a restored
backup, an old node syncing), so they run idempotently on every
_apply_schema rather than once per schema version.

They used to live inline in database._apply_schema, several inside their
own `except Exception: pass` — which would also have swallowed a locked
database or a disk error. The policy now is explicit and uniform:

- a missing table/column is benign (that schema simply isn't here yet);
- ANY other failure aborts startup, the same fail-closed stance as
  migrations.apply_migration.
"""
from __future__ import annotations

import logging
import sqlite3

log = logging.getLogger(__name__)


def execute_tolerant(conn, stmt: str, name: str) -> None:
    """Run one backfill statement under the explicit policy above."""
    try:
        conn.execute(stmt)
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "no such table" in msg or "no such column" in msg:
            log.debug("[backfill %s] skipped (schema not present): %s", name, e)
            return
        raise SystemExit(
            f"FATAL: data backfill '{name}' failed and was NOT a benign "
            "missing-schema case. Refusing to start rather than trade on "
            f"partially corrected data.\n  statement: {stmt.strip()[:120]}\n  error: {e}"
        )


def _order_type_limit(conn) -> None:
    """Trades that filled before the order_type column existed defaulted to
    'market'. Limit Runner / ORB's identity alone proves they were resting
    pending orders (pending_placed_at is unrecoverable — stays blank)."""
    execute_tolerant(
        conn,
        "UPDATE vantage_simulated_trades SET order_type='limit' "
        "WHERE strategy IN ('limit_runner','orb_fixed') AND order_type='market'",
        "order_type_limit",
    )


def _rebrand_strategy_ids(conn) -> None:
    """2026-07-23 rebrand: rows stored under 'gd_vip_runner' must keep
    pointing at the same management logic under 'reversal_runner', or they
    silently fall back to the global default strategy on next read."""
    for tbl, col in (
        ("vantage_risk_settings", "trade_strategy"),
        ("vantage_signals", "strategy"),
        ("vantage_simulated_trades", "strategy"),
        ("vantage_pending_orders", "strategy"),
        ("channel_performance", "strategy_override"),
        ("channel_strategy_rec", "strategy"),
        ("strategy_param_templates", "strategy"),
    ):
        execute_tolerant(
            conn,
            f"UPDATE {tbl} SET {col}='reversal_runner' WHERE {col}='gd_vip_runner'",
            f"rebrand_strategy_ids:{tbl}.{col}",
        )


def _rebrand_source_names(conn) -> None:
    """Same rebrand, for the source/channel-name string itself — without it,
    historical rows show as a second orphaned 'GD Copy Engine' row with none
    of the new row's override/stats history."""
    for tbl, col in (
        ("channel_parser_config", "channel_name"),
        ("channel_performance", "source"),
        ("channel_strategy_rec", "source"),
        ("vantage_simulated_trades", "tg_source"),
        ("vantage_signals", "source_name"),
        ("vantage_pending_orders", "channel_name"),
        ("consolidated_trades", "tg_source"),
    ):
        execute_tolerant(
            conn,
            f"UPDATE {tbl} SET {col}='Reversal Engine' WHERE {col}='GD Copy Engine'",
            f"rebrand_source_names:{tbl}.{col}",
        )


def _gd2_instant_entry(conn) -> None:
    """Enable instant_entry for GD2 channel configs bootstrapped before GD2
    IME support existed (they defaulted to 0)."""
    execute_tolerant(
        conn,
        "UPDATE channel_parser_config SET instant_entry_enabled=1 "
        "WHERE parser_format='gd2' AND instant_entry_enabled=0",
        "gd2_instant_entry",
    )


def _strip_instant_prefix(conn) -> None:
    """Strip the legacy 'instant:' prefix from source names."""
    execute_tolerant(
        conn,
        "UPDATE vantage_simulated_trades SET tg_source = SUBSTR(tg_source, 9) "
        "WHERE tg_source LIKE 'instant:%'",
        "strip_instant_prefix:trades",
    )
    execute_tolerant(
        conn,
        "UPDATE vantage_signals SET source_name = SUBSTR(source_name, 9) "
        "WHERE source_name LIKE 'instant:%'",
        "strip_instant_prefix:signals",
    )


def _dpm_tg_source(conn) -> None:
    """Backfill tg_source into dpm_trade_performance from the trade record."""
    execute_tolerant(
        conn,
        "UPDATE dpm_trade_performance SET tg_source = ("
        "    SELECT tg_source FROM vantage_simulated_trades t "
        "    WHERE t.trade_id = dpm_trade_performance.trade_id"
        ") WHERE tg_source IS NULL OR tg_source LIKE 'instant:%'",
        "dpm_tg_source",
    )


# Applied in order on every boot. Order matters: the rebrands run before the
# DPM backfill so the copied tg_source is already the corrected string, and
# order_type/instant-prefix corrections keep their historical position.
BACKFILLS: list = [
    ("order_type_limit", _order_type_limit),
    ("rebrand_strategy_ids", _rebrand_strategy_ids),
    ("rebrand_source_names", _rebrand_source_names),
    ("gd2_instant_entry", _gd2_instant_entry),
    ("strip_instant_prefix", _strip_instant_prefix),
    ("dpm_tg_source", _dpm_tg_source),
]


def run(conn) -> None:
    for _name, fn in BACKFILLS:
        fn(conn)
