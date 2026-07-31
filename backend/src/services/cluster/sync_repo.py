"""Sync — split from core/database.py.
Extracted from forex_trader/core/database.py -- see
docs/todo/refactor/core-database-migration/. Verbatim port: same functions,
same SQL, same behavior, using database.py's own db()/to_db_thread()
machinery (unchanged, already correct -- this is a pure file-size split,
not a connection-layer migration). Re-exported from database.py so every
existing `db_module.<name>` call site works completely unchanged.
"""
import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

from forex_trader.core.database import db, row_to_dict, to_db_thread, _schedule_coro  # noqa: E402
from backend.src.services.risk.app_config_repo import get_app_config, set_app_config  # noqa: E402
from backend.src.services.risk.risk_settings_repo import get_risk_settings  # noqa: E402

# ── Local/Remote sync — consolidated ledger, active-trader flag, node config ──
# Additive only: this never touches the per-engine operational tables
# (bo_signals, test_signals, re_signals, vantage_simulated_trades). Those
# use autoincrement integer ids that would collide if two independent
# installs' rows were ever merged directly. consolidated_trades is a
# separate, append-only table keyed by (node_id, trade_id) — trade_id is
# already a UUID on every engine that pushes here, so cross-node collision
# is not possible.

def _ensure_sync_tables() -> None:
    with db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS consolidated_trades (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                node_id      TEXT    NOT NULL,
                trade_id     TEXT    NOT NULL,
                engine       TEXT    NOT NULL,
                direction    TEXT    NOT NULL,
                strategy     TEXT,
                open_time    REAL,
                close_time   REAL,
                pnl_dollars  REAL,
                outcome      TEXT,
                received_at  REAL    NOT NULL,
                UNIQUE(node_id, trade_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_consolidated_close ON consolidated_trades(close_time)"
        )
        # tg_source (channel) + mt5_ticket — added after the initial ledger
        # design shipped without them. History's ticket->channel lookup is
        # keyed by mt5_ticket and built only from THIS node's own
        # vantage_simulated_trades, so a trade opened by the OTHER node
        # (real, same shared MT5 account, but no local record of it) showed
        # a blank channel. Both columns let history.py fall back to the
        # ledger for tickets it has no local record of.
        for col, defn in [
            ("tg_source",  "TEXT"),
            ("mt5_ticket", "INTEGER"),
        ]:
            try:
                conn.execute(f"ALTER TABLE consolidated_trades ADD COLUMN {col} {defn}")
            except Exception:
                pass  # column already exists
        # max_tp_hit + rr — same cross-node gap as tg_source/mt5_ticket above:
        # History's Max TP Hit and R:R columns are built only from THIS node's
        # own vantage_simulated_trades, so a trade the OTHER node opened
        # showed blank for both, even though the underlying MT5 deal (and its
        # cost) is on the one shared account. max_tp_hit isn't known at the
        # original push_trade_closed() call (it's computed by
        # _max_tp_checker_loop 30+ min after close) — see that loop's
        # follow-up push, which relies on the same UNIQUE(node_id, trade_id)
        # upsert updating this row rather than creating a duplicate.
        for col, defn in [
            ("max_tp_hit", "TEXT"),
            ("rr",         "REAL"),
        ]:
            try:
                conn.execute(f"ALTER TABLE consolidated_trades ADD COLUMN {col} {defn}")
            except Exception:
                pass  # column already exists
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_consolidated_ticket ON consolidated_trades(mt5_ticket)"
        )


def get_or_create_node_id() -> str:
    """Stable random identifier for this install, generated once. Used to tag
    rows pushed into the other side's consolidated ledger."""
    existing = get_app_config("sync_node_id")
    if existing:
        return existing
    import uuid as _uuid
    node_id = _uuid.uuid4().hex[:12]
    set_app_config("sync_node_id", node_id)
    return node_id


def record_consolidated_trade(node_id: str, trade: dict) -> None:
    """Insert or update one closed-trade row in the consolidated ledger.
    Safe to call for both locally-originated trades and rows received from
    the other node over the sync channel — UNIQUE(node_id, trade_id) makes
    this idempotent, so re-delivery after a reconnect can't duplicate a row.

    Also the target of _max_tp_checker_loop's follow-up push once
    max_tp_hit is known (30+ min after the original close-time push, which
    can't include it yet) — the ON CONFLICT UPDATE only touches max_tp_hit
    when a real value is supplied, so that second, partial push can't
    clobber rr or any other field back to NULL."""
    _ensure_sync_tables()
    with db() as conn:
        conn.execute(
            """INSERT INTO consolidated_trades
               (node_id, trade_id, engine, direction, strategy, open_time,
                close_time, pnl_dollars, outcome, received_at, tg_source, mt5_ticket,
                max_tp_hit, rr)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(node_id, trade_id) DO UPDATE SET
                 close_time=excluded.close_time, pnl_dollars=excluded.pnl_dollars,
                 outcome=excluded.outcome, received_at=excluded.received_at,
                 tg_source=excluded.tg_source, mt5_ticket=excluded.mt5_ticket,
                 max_tp_hit=COALESCE(excluded.max_tp_hit, consolidated_trades.max_tp_hit),
                 rr=COALESCE(excluded.rr, consolidated_trades.rr)""",
            (node_id, trade["trade_id"], trade.get("engine", ""),
             trade.get("direction", ""), trade.get("strategy", ""),
             trade.get("open_time"), trade.get("close_time"),
             trade.get("pnl_dollars"), trade.get("outcome"), time.time(),
             trade.get("tg_source"), trade.get("mt5_ticket"),
             trade.get("max_tp_hit"), trade.get("rr")),
        )


def get_consolidated_ticket_maps() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """Return ({mt5_ticket_str: tg_source}, {mt5_ticket_str: strategy},
    {mt5_ticket_str: direction}) from the consolidated ledger — i.e. from ALL
    paired nodes, not just this one. history.py merges this in as a fallback
    for tickets with no local vantage_simulated_trades row (trades the OTHER
    node opened)."""
    _ensure_sync_tables()
    with db() as conn:
        rows = conn.execute(
            "SELECT mt5_ticket, tg_source, strategy, direction FROM consolidated_trades "
            "WHERE mt5_ticket IS NOT NULL"
        ).fetchall()
    channel_map, strategy_map, direction_map = {}, {}, {}
    for mt5_ticket, tg_source, strategy, direction in rows:
        key = str(mt5_ticket)
        if tg_source:
            channel_map[key] = tg_source
        if strategy:
            strategy_map[key] = strategy
        if direction:
            direction_map[key] = direction
    return channel_map, strategy_map, direction_map


def get_consolidated_extra_maps() -> tuple[dict[str, str], dict[str, float]]:
    """Return ({mt5_ticket_str: max_tp_hit}, {mt5_ticket_str: rr}) from the
    consolidated ledger — same cross-node fallback purpose as
    get_consolidated_ticket_maps(), split out separately since these two
    columns were added later and populate at different times (rr at close,
    max_tp_hit 30+ min after)."""
    _ensure_sync_tables()
    with db() as conn:
        rows = conn.execute(
            "SELECT mt5_ticket, max_tp_hit, rr FROM consolidated_trades "
            "WHERE mt5_ticket IS NOT NULL AND (max_tp_hit IS NOT NULL OR rr IS NOT NULL)"
        ).fetchall()
    max_tp_map, rr_map = {}, {}
    for mt5_ticket, max_tp_hit, rr in rows:
        key = str(mt5_ticket)
        if max_tp_hit:
            max_tp_map[key] = max_tp_hit
        if rr is not None:
            rr_map[key] = rr
    return max_tp_map, rr_map


def get_consolidated_trades(days: int = 0) -> list[dict]:
    _ensure_sync_tables()
    cutoff = time.time() - days * 86400 if days > 0 else 0
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM consolidated_trades WHERE COALESCE(close_time,0) >= ? "
            "ORDER BY close_time DESC", (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_active_trader() -> str:
    """'local' or 'remote_vps'. Defaults to remote_vps — the VPS is the
    always-on trader unless this Mac has explicitly taken over."""
    return get_app_config("active_trader") or "remote_vps"


def set_active_trader(value: str) -> None:
    set_app_config("active_trader", value)


def is_remote_node() -> bool:
    """True iff this process is running in the VPS/remote-server sync role
    (sync_server_enabled) — a fixed physical-machine fact, independent of
    which side is currently the active trader or whether centralized signal
    generation is toggled on. Used to keep the Breakout, Bounce, and Reversal Engine
    analytical signal generators local-node-only unconditionally, rather than
    only as a side effect of centralized_signal_gen_enabled staying on (see
    should_generate_signals_here below, which is execution-routing-focused
    and stays conditional)."""
    return get_app_config("sync_server_enabled") == "1"


def should_generate_signals_here() -> bool:
    """False only when centralized_signal_gen_enabled is on AND this node is
    physically the VPS AND the VPS is currently the active trader (Remote
    mode) — i.e. signal generation has been centralized onto the Mac and
    this node should only execute trades forwarded to it, not analyze
    anything itself. Local mode and centralization-off both return True
    (today's behavior: this node generates as normal)."""
    rs = get_risk_settings()
    if not rs.get("centralized_signal_gen_enabled"):
        return True
    try:
        from backend.src.controllers.sync import server as _sync_srv_mod
    except ImportError:
        return True
    if _sync_srv_mod.get_instance() is None:
        return True  # this is the Mac — always generate
    if get_active_trader() != "remote_vps":
        return True  # VPS is standing down anyway (Local mode) — unaffected
    return False


def get_stood_down_engines() -> list[str]:
    """Engines this node auto-paused because the OTHER node took over via
    STAND_DOWN — distinct from engines the local user disabled themselves,
    so RESUME only re-enables what sync stood down, never overriding a
    deliberate local on/off preference."""
    import json as _json
    raw = get_app_config("sync_stood_down_engines")
    try:
        return _json.loads(raw) if raw else []
    except Exception:
        return []


def set_stood_down_engines(names: list[str]) -> None:
    import json as _json
    set_app_config("sync_stood_down_engines", _json.dumps(names))


def generate_sync_token() -> str:
    """Generate a new random sync token, persist it encrypted (reusing the
    Fernet-at-rest pattern from core.secrets), and return the plaintext once
    so the operator can copy it into the other node's settings. Overwrites
    any previous token — do this once per node pairing, not per restart."""
    import secrets as _secrets
    from backend.src.config import secrets as _sec
    token = _secrets.token_urlsafe(32)
    set_app_config("sync_token_enc", _sec.encrypt(token))
    return token


def get_sync_token() -> str:
    """Decrypt and return this node's persisted sync token, or '' if none
    has been generated yet."""
    from backend.src.config import secrets as _sec
    enc = get_app_config("sync_token_enc") or ""
    return _sec.decrypt(enc) if enc else ""
