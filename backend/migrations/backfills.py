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


def _once_marker(conn, key: str) -> bool:
    """True when this once-only backfill has already run on this database.

    Some corrections cannot be made idempotent by their WHERE clause -- there
    is no way to tell an already-corrected value from one a user has since
    typed by hand -- so they are gated on an app_config marker instead and
    run exactly once, ever. Added by the 2026-08-25 upstream merge."""
    try:
        return conn.execute(
            "SELECT 1 FROM app_config WHERE key=?", (key,)
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def _set_once_marker(conn, key: str) -> None:
    try:
        conn.execute(
            "INSERT OR REPLACE INTO app_config (key, value) VALUES (?, ?)", (key, "1")
        )
    except sqlite3.OperationalError:
        pass


def _gd2_instant_entry(conn) -> None:
    """Enable instant_entry for GD2 channel configs bootstrapped before GD2
    IME support existed (they defaulted to 0).

    ONCE, not on every boot (upstream 2026-08-14). This ran unconditionally
    and run() fires on every app start, so it re-enabled IME for every gd2
    channel every time -- silently reverting the setting within seconds of it
    being turned off, which made Immediate Market Entry impossible to disable
    on a gd2 channel at all, through the UI or otherwise. Found while turning
    IME off for GOLD DIGGERS INSTITUTIONAL, where market entry backtests at
    -0.03R against +0.26R for the limit entry the channel actually publishes.

    New rows do not need it: the runtime's auto-bootstrap already defaults
    gd2 to instant_entry_enabled=1 at first sight."""
    key = "gd2_ime_backfill_done"
    if _once_marker(conn, key):
        return
    execute_tolerant(
        conn,
        "UPDATE channel_parser_config SET instant_entry_enabled=1 "
        "WHERE parser_format='gd2' AND instant_entry_enabled=0",
        "gd2_instant_entry",
    )
    _set_once_marker(conn, key)


def _gd2_channel_rename_heal(conn) -> None:
    """Fold "Gold Diggers 2.0"'s orphaned rows into GOLD DIGGERS INSTITUTIONAL.

    Those channel_performance / channel_strategy_rec rows predate both the
    rename cascade and the PK-collision fix in sync_channel_rename, whose
    plain UPDATE silently no-ops when the canonical row already exists. So
    they were never folded in and every lookup by the live channel name
    missed them -- a user-set EA Template override sat here invisibly and the
    channel silently traded under the global default strategy instead.
    sync_channel_rename will not re-fire for this pair on its own (the
    Telegram-side title mismatch that triggered it is long gone), so heal it
    directly, once, with the same merge-safe helper.
    Upstream 2026-07-27, re-homed by the 2026-08-25 merge."""
    try:
        from backend.src.services.channels.repo import (
            _fold_renamed_row, _CHANNEL_UNIQUE_TABLES,
        )
    except ImportError:
        log.debug("[backfill gd2_channel_rename_heal] fold helper unavailable")
        return
    for tbl, col in (
        ("channel_parser_config", "channel_name"),
        ("channel_performance", "source"),
        ("channel_strategy_rec", "source"),
    ):
        try:
            # No conn: the helper opens its own transaction, which nests into
            # whatever this backfill is already inside.
            _fold_renamed_row(
                tbl, col, "Gold Diggers 2.0", "GOLD DIGGERS INSTITUTIONAL",
                _CHANNEL_UNIQUE_TABLES.get(tbl, ()),
            )
        except sqlite3.OperationalError as e:
            log.debug("[backfill gd2_channel_rename_heal] %s skipped: %s", tbl, e)


def _anchor_tp_pips_units(conn) -> None:
    """One-off unit fix: Anchor TP ladder tp{n}_pips were raw points, not pips.

    1 pip is 0.10 price on this XAUUSD feed (10 * _Point, _Point=0.01) -- the
    same conversion ForexTraderBridge.mq5's PipsToPrice() already applied
    correctly for the Pending ladder. Root-caused live 2026-07-31 (ticket
    1689710560): tp1_pips=30 sent TP1 to entry+30.0 (300 pips) instead of
    entry+3.0 (30 pips, what the channel's "+30 PIPS" wording means).

    Fixing the conversion without this backfill would silently move every
    existing template's TPs 10x closer than they trade today. Marker-gated
    rather than WHERE-shaped: there is no way to tell an already-migrated 30
    from a 30 someone deliberately enters after this ships.
    Upstream 2026-07-31, re-homed by the 2026-08-25 merge."""
    key = "anchor_tp_pips_migrated_2026_07_31"
    if _once_marker(conn, key):
        return
    for n in range(1, 9):
        execute_tolerant(
            conn,
            f"UPDATE ea_trade_templates SET tp{n}_pips = tp{n}_pips * 10 "
            f"WHERE tp{n}_pips != 0",
            f"anchor_tp_pips_units:tp{n}",
        )
    _set_once_marker(conn, key)


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
    # Re-homed from upstream's core/database.py _apply_schema tail by the
    # 2026-08-25 merge. Both run after the rebrands, which is where they sat
    # upstream: the rename heal folds rows the rebrands have already renamed.
    ("gd2_channel_rename_heal", _gd2_channel_rename_heal),
    ("anchor_tp_pips_units", _anchor_tp_pips_units),
]


def run(conn) -> None:
    for _name, fn in BACKFILLS:
        fn(conn)
