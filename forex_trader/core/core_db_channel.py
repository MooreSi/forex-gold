"""Channel — split from core/database.py.
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
from forex_trader.core.core_db_sync import _ensure_sync_tables  # noqa: E402
from forex_trader.core.core_db_analytics import _session_for_hour, _trade_pts  # noqa: E402

_TG_GROUP_ID_MAP: dict[str, str] = {
    "1608388054": "Gold Diggers VIP",
    "2616846888": "GOLD DIGGERS 2.0 ⚡️",
}


def _normalise_tg_source(src: str) -> str:
    """Map raw numeric Telegram group IDs to human-readable channel names."""
    return _TG_GROUP_ID_MAP.get(str(src).strip(), src) if src else src


# Channel-name tables that key rows by the channel's display name string
# rather than its stable numeric Telegram group ID -- every one of these
# needs the same rename applied together, or a renamed channel silently
# forks into two disconnected rows (old name keeps its override/history,
# new name starts blank) instead of continuing under its existing settings.
_CHANNEL_NAME_TABLES = (
    ("channel_parser_config", "channel_name"),
    ("channel_performance", "source"),
    ("channel_strategy_rec", "source"),
    ("vantage_simulated_trades", "tg_source"),
    ("vantage_pending_orders", "channel_name"),
    ("consolidated_trades", "tg_source"),
)


def sync_channel_rename(old_name: str, new_name: str) -> None:
    """Cascade a channel display-name change (e.g. the Telegram group's real
    title was edited, or a signal generator's own name changed) across every
    table that keys rows by that name string. Called automatically when the
    Telegram reader notices its configured group's live title no longer
    matches what was stored (see telegram_reader.TelegramReader._resolve_entity),
    and can also be called for internal signal-source renames. No-op if the
    names are equal or either is empty."""
    old_name = (old_name or "").strip()
    new_name = (new_name or "").strip()
    if not old_name or not new_name or old_name == new_name:
        return
    with db() as conn:
        for tbl, col in _CHANNEL_NAME_TABLES:
            try:
                conn.execute(f"UPDATE {tbl} SET {col}=? WHERE {col}=?", (new_name, old_name))
            except Exception:
                pass  # table/column doesn't exist on this schema version
    # If the old name was itself one of the fixed canonical bucket names (the
    # Channel Strategy tab's five rows), swap the bucket to the new name in
    # place -- otherwise get_all_channel_strategy_settings() would keep
    # looking for the old, now-nonexistent name and the renamed channel's
    # freshly-updated rows above would have nowhere to bucket into.
    if old_name in CANONICAL_CHANNEL_ORDER:
        CANONICAL_CHANNEL_ORDER[CANONICAL_CHANNEL_ORDER.index(old_name)] = new_name
    # Every existing variant that used to resolve to old_name must now
    # resolve to new_name instead, and the new live title itself must map
    # to new_name (self-map) so future messages/trades canonicalise correctly.
    for variant, canon in list(CANONICAL_CHANNELS.items()):
        if canon == old_name:
            CANONICAL_CHANNELS[variant] = new_name
    CANONICAL_CHANNELS[new_name] = new_name
    log.info("[Channel] Renamed channel '%s' -> '%s' across all tracking tables", old_name, new_name)


def get_channel_scorecard(days: int = 30) -> list[dict]:
    """Per signal-source performance over the last `days`, sorted by net P&L desc.
    Each row: source, trades, wins, losses, win_rate, avg_pts, payoff_rr,
    net_pnl, and a per-session {london/ny/overlap/asian: net_pnl} split.

    Merges in the consolidated ledger as a fallback for trades the OTHER
    paired node closed (e.g. Reversal Engine's live-executed trades, which
    live in reversal_engine's own DB and never get a vantage_simulated_trades
    row at all — see reversal_engine/engine.py's _reconcile_live_signal,
    which pushes to the ledger via push_trade_closed regardless of which
    node ran the trade). Ledger rows have no entry_price/close_price, so
    their points-based contribution (avg_pts/payoff_rr) is 0 — win/loss
    counts and net_pnl, the primary numbers here, are unaffected."""
    import time as _t
    from datetime import datetime as _dt, timezone as _tz
    _ensure_sync_tables()
    cutoff = _t.time() - days * 86400
    with db() as conn:
        rows = conn.execute(
            "SELECT tg_source, direction, entry_price, close_price, net_pnl, close_time, trade_id "
            "FROM vantage_simulated_trades "
            "WHERE status='closed' AND mt5_ticket IS NOT NULL AND close_time >= ?",
            (cutoff,),
        ).fetchall()
        ledger_rows = conn.execute(
            "SELECT tg_source, direction, pnl_dollars, close_time, trade_id "
            "FROM consolidated_trades WHERE mt5_ticket IS NOT NULL AND close_time >= ?",
            (cutoff,),
        ).fetchall()

    local_ids = {r[6] for r in rows}
    agg: dict[str, dict] = {}
    for tg_source, direction, entry, close, pnl, ct, _tid in rows:
        src = _normalise_tg_source(tg_source or "Manual Signal")
        a = agg.setdefault(src, {
            "source": src, "trades": 0, "wins": 0, "losses": 0,
            "net_pnl": 0.0, "win_pts": [], "loss_pts": [], "all_pts": [],
            "sessions": {"london": 0.0, "ny": 0.0, "overlap": 0.0, "asian": 0.0},
        })
        pnl = float(pnl or 0)
        pts = _trade_pts(direction, float(entry or 0), float(close or 0))
        a["trades"]  += 1
        a["net_pnl"] += pnl
        a["all_pts"].append(pts)
        if pnl > 0:
            a["wins"] += 1
            a["win_pts"].append(pts)
        elif pnl < 0:
            a["losses"] += 1
            a["loss_pts"].append(abs(pts))
        if ct:
            sess = _session_for_hour(_dt.fromtimestamp(float(ct), tz=_tz.utc).hour)
            a["sessions"][sess] += pnl

    for tg_source, direction, pnl, ct, tid in ledger_rows:
        if tid in local_ids:
            continue
        src = _normalise_tg_source(tg_source or "Manual Signal")
        a = agg.setdefault(src, {
            "source": src, "trades": 0, "wins": 0, "losses": 0,
            "net_pnl": 0.0, "win_pts": [], "loss_pts": [], "all_pts": [],
            "sessions": {"london": 0.0, "ny": 0.0, "overlap": 0.0, "asian": 0.0},
        })
        pnl = float(pnl or 0)
        a["trades"]  += 1
        a["net_pnl"] += pnl
        if pnl > 0:
            a["wins"] += 1
        elif pnl < 0:
            a["losses"] += 1
        if ct:
            sess = _session_for_hour(_dt.fromtimestamp(float(ct), tz=_tz.utc).hour)
            a["sessions"][sess] += pnl

    out = []
    for a in agg.values():
        decided = a["wins"] + a["losses"]
        wr  = (a["wins"] / decided * 100) if decided else 0.0
        avg_pts = (sum(a["all_pts"]) / len(a["all_pts"])) if a["all_pts"] else 0.0
        avg_win  = (sum(a["win_pts"]) / len(a["win_pts"])) if a["win_pts"] else 0.0
        avg_loss = (sum(a["loss_pts"]) / len(a["loss_pts"])) if a["loss_pts"] else 0.0
        payoff = round(avg_win / avg_loss, 2) if avg_loss > 0 else 0.0
        out.append({
            "source": a["source"], "trades": a["trades"],
            "wins": a["wins"], "losses": a["losses"],
            "win_rate": round(wr, 1), "avg_pts": round(avg_pts, 2),
            "payoff_rr": payoff, "net_pnl": round(a["net_pnl"], 2),
            "sessions": {k: round(v, 2) for k, v in a["sessions"].items()},
        })
    out.sort(key=lambda r: r["net_pnl"], reverse=True)
    return out


_CHANNEL_MIN_SAMPLE   = 8    # need this many decided trades before adapting


_CHANNEL_PAUSE_PF     = 0.0  # auto-pause disabled (set > 0 to re-enable)


_CHANNEL_NO_AUTO_PAUSE = {"Signal Generator", "Bounce Generator", "manual_market"}


def _channel_profit_factor(r: dict) -> float:
    """Compute dollar profit factor from scorecard row.
    PF = (win_rate × payoff_rr) / (1 − win_rate/100)  [points-normalised approximation].
    Returns 0.0 when there is no loss history."""
    wr  = r["win_rate"] / 100.0
    rr  = r.get("payoff_rr", 0.0)
    if wr >= 1.0 or rr <= 0:
        return 0.0 if rr <= 0 else 99.0
    return round((wr * rr) / (1.0 - wr), 3)


def recompute_channel_performance(days: int = 30) -> list[str]:
    """Recompute per-channel lot multipliers and auto-pause flags from rolling
    `days` performance, and upsert into channel_performance. Rows with
    manual_override=1 keep their paused flag (set by the user) untouched.

    Returns a list of source names that were newly auto-paused this run,
    so callers can send a Telegram alert."""
    import time as _t
    scorecard = get_channel_scorecard(days)
    now = _t.time()
    newly_paused: list[str] = []
    with db() as conn:
        for r in scorecard:
            src = r["source"]
            n   = r["wins"] + r["losses"]
            pf  = _channel_profit_factor(r)
            wr  = r["win_rate"]

            if n < _CHANNEL_MIN_SAMPLE or src in _CHANNEL_NO_AUTO_PAUSE:
                lot_mult, auto_pause = 1.0, 0
            elif pf < _CHANNEL_PAUSE_PF:
                lot_mult, auto_pause = 0.5, 1
            elif wr < 55.0:
                lot_mult, auto_pause = 1.0, 0
            else:
                lot_mult, auto_pause = 1.3, 0

            row = conn.execute(
                "SELECT manual_override, paused FROM channel_performance WHERE source=?",
                (src,),
            ).fetchone()
            if row and row[0]:
                paused = row[1]  # user override always wins
            else:
                was_paused = bool(row[1]) if row else False
                paused = auto_pause
                if auto_pause and not was_paused:
                    newly_paused.append(src)

            conn.execute(
                "INSERT INTO channel_performance "
                "(source, lot_mult, win_rate, sample_n, net_pnl, paused, manual_override, updated_at) "
                "VALUES (?,?,?,?,?,?,COALESCE((SELECT manual_override FROM channel_performance WHERE source=?),0),?) "
                "ON CONFLICT(source) DO UPDATE SET "
                "  lot_mult=excluded.lot_mult, win_rate=excluded.win_rate, "
                "  sample_n=excluded.sample_n, net_pnl=excluded.net_pnl, "
                "  paused=?, updated_at=excluded.updated_at",
                (src, lot_mult, wr, n, r["net_pnl"], paused, src, now, paused),
            )
    return newly_paused


def get_channel_lot_mult(source: str) -> tuple[float, bool]:
    """Return (lot_multiplier, paused) for a signal source. Defaults (1.0, False)."""
    if not source:
        return 1.0, False
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT lot_mult, paused FROM channel_performance WHERE source=?",
                (source,),
            ).fetchone()
        if not row:
            return 1.0, False
        return float(row[0] or 1.0), bool(row[1])
    except Exception:
        return 1.0, False


_CHANNEL_TRUST_MIN_SAMPLES = 20   # minimum closed trades to consider a channel trusted


_CHANNEL_TRUST_MIN_WR      = 50.0 # minimum win-rate % (stored as 50.0 = 50%)


def get_channel_trust(source: str) -> bool:
    """Return True if a channel has a statistically proven, net-profitable track record.

    Trust criteria (must pass all three):
      - sample_n >= 20 (enough data to be meaningful)
      - win_rate >= 50% (more wins than losses, column stores 55.9 for 55.9%)
      - net_pnl > 0 (actually made money in our system)

    Checks both the raw channel name and the 'Telegram Auto (<name>)' variant
    that the auto-execution path uses when logging trades, since signals arrive
    under the channel name but executed trades may be stored under the prefixed form.
    """
    if not source:
        return False
    candidates = [source, f"Telegram Auto ({source})"]
    try:
        with db() as conn:
            for candidate in candidates:
                row = conn.execute(
                    "SELECT win_rate, sample_n, net_pnl FROM channel_performance WHERE source=?",
                    (candidate,),
                ).fetchone()
                if not row:
                    continue
                if (
                    (row[1] or 0) >= _CHANNEL_TRUST_MIN_SAMPLES
                    and (row[0] or 0.0) >= _CHANNEL_TRUST_MIN_WR
                    and (row[2] or 0.0) > 0
                ):
                    return True
    except Exception:
        pass
    return False


CANONICAL_CHANNELS: dict[str, str] = {
    # Gold Diggers 2.0 variants
    "GOLD DIGGERS 2.0 ⚡️":           "Gold Diggers 2.0",
    "Telegram Auto (GOLD DIGGERS 2.0 ⚡️)": "Gold Diggers 2.0",
    "2616846888":                               "Gold Diggers 2.0",
    "Gold Diggers 2.0":                         "Gold Diggers 2.0",
    # Gold Diggers VIP variants
    "Gold Diggers VIP":                         "Gold Diggers VIP",
    "Telegram Auto (Gold Diggers VIP)":         "Gold Diggers VIP",
    "1608388054":                               "Gold Diggers VIP",
    # Signal generators
    "Signal Generator":                         "Bounce Engine",   # legacy tg_source
    "Bounce Generator":                         "Bounce Engine",
    "Bounce Engine":                            "Bounce Engine",
    "Breakout Engine":                          "Breakout Engine",
    # Reversal Engine
    "Reversal Engine":                           "Reversal Engine",
    "Gold Diggers VIP Copy":                    "Reversal Engine",
}


CANONICAL_CHANNEL_ORDER = [
    "Gold Diggers 2.0",
    "Gold Diggers VIP",
    "Reversal Engine",
    "Bounce Engine",
    "Breakout Engine",
]


def _canonical(source: str) -> str:
    """Return the canonical channel name for a raw tg_source / source string."""
    return CANONICAL_CHANNELS.get(source, source)


def get_channel_strategy_override(source: str):
    """Return the per-channel strategy override string, or None (inherit global).

    Returns 'auto' when auto-Claude mode is active, a strategy key when manually
    overridden, or None to fall back to the global Active Strategy setting.
    Looks up using the canonical channel name so all variants share one setting.
    """
    if not source:
        return None
    canon = _canonical(source)
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT strategy_override, auto_strategy FROM channel_performance WHERE source=?",
                (canon,),
            ).fetchone()
        if not row:
            return None
        if row[1]:      # auto_strategy flag set
            return "auto"
        return row[0]   # strategy_override TEXT (may be None)
    except Exception:
        return None


_applying_sync_channel_strategy = False  # re-entrancy guard — see update_risk_settings


def set_channel_strategy_override(
    source: str, strategy: str | None, auto: bool = False, _from_sync: bool = False,
) -> None:
    """Set per-channel strategy.  strategy=None + auto=False clears override (inherit global).

    _from_sync=True is used only when APPLYING a value that arrived over the
    Local/Remote sync channel — without this guard, applying an incoming
    sync value would immediately re-forward it back out, an infinite
    propose/confirm ping-pong between the two nodes (same pattern as
    update_risk_settings above)."""
    global _applying_sync_channel_strategy
    import time as _t
    canon = _canonical(source)
    with db() as conn:
        conn.execute(
            "INSERT INTO channel_performance (source, strategy_override, auto_strategy, updated_at) "
            "VALUES (?,?,?,?) "
            "ON CONFLICT(source) DO UPDATE SET strategy_override=excluded.strategy_override, "
            "  auto_strategy=excluded.auto_strategy, updated_at=excluded.updated_at",
            (canon, strategy, 1 if auto else 0, _t.time()),
        )
    if not _from_sync and not _applying_sync_channel_strategy:
        _applying_sync_channel_strategy = True
        try:
            _forward_channel_strategy_over_sync(canon, strategy, auto)
        finally:
            _applying_sync_channel_strategy = False


def get_all_channel_strategy_overrides() -> dict[str, dict]:
    """Lightweight {canonical_source: {strategy, auto}} snapshot for sync —
    just the override fields, not the per-node performance stats that
    get_all_channel_strategy_settings() also returns."""
    result = {ch: {"strategy": None, "auto": False} for ch in CANONICAL_CHANNEL_ORDER}
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT source, strategy_override, auto_strategy FROM channel_performance"
            ).fetchall()
        for source, strategy, auto in rows:
            if source in result:  # canonical rows only — variants don't carry the override
                result[source] = {"strategy": strategy, "auto": bool(auto)}
    except Exception:
        pass
    return result


def _forward_channel_strategy_over_sync(source: str, strategy: str | None, auto: bool) -> None:
    """Send a locally-made channel-strategy change to the paired node,
    whichever role this process has. No-op if sync isn't configured."""
    try:
        from forex_trader.sync import client as _sync_cli_mod
        cli = _sync_cli_mod.get_instance()
        if cli is not None:
            _schedule_coro(cli.propose_channel_strategy(source, strategy, auto))
            return
    except Exception as e:
        log.debug("[Sync] channel strategy forward (client) failed: %s", e)

    try:
        from forex_trader.sync import server as _sync_srv_mod
        srv = _sync_srv_mod.get_instance()
        if srv is not None:
            _schedule_coro(srv.broadcast_channel_strategy())
    except Exception as e:
        log.debug("[Sync] channel strategy forward (server) failed: %s", e)


def get_channel_strategy_rec(source: str) -> dict:
    """Return {strategy, reasoning, confidence, updated_at} for the last Claude recommendation."""
    canon = _canonical(source)
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT strategy, reasoning, confidence, updated_at FROM channel_strategy_rec WHERE source=?",
                (canon,),
            ).fetchone()
        if row:
            return {"strategy": row[0], "reasoning": row[1],
                    "confidence": row[2], "updated_at": row[3]}
    except Exception:
        pass
    return {"strategy": "", "reasoning": "", "confidence": 0.0, "updated_at": 0.0}


def set_channel_strategy_rec(source: str, strategy: str, reasoning: str, confidence: float) -> None:
    """Upsert a Claude strategy recommendation for a channel."""
    import time as _t
    canon = _canonical(source)
    with db() as conn:
        conn.execute(
            "INSERT INTO channel_strategy_rec (source, strategy, reasoning, confidence, updated_at) "
            "VALUES (?,?,?,?,?) "
            "ON CONFLICT(source) DO UPDATE SET strategy=excluded.strategy, "
            "  reasoning=excluded.reasoning, confidence=excluded.confidence, "
            "  updated_at=excluded.updated_at",
            (canon, strategy, reasoning, round(confidence, 3), _t.time()),
        )


def get_open_trade_count() -> int:
    """Count all open trades regardless of channel/source."""
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM vantage_simulated_trades WHERE status='open'"
            ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def get_open_trade_count_for_channel(source: str) -> int:
    """Count open trades whose tg_source matches the given channel (all canonical variants)."""
    canon = _canonical(source)
    variants = [s for s, c in CANONICAL_CHANNELS.items() if c == canon]
    if not variants:
        variants = [source]
    placeholders = ",".join("?" * len(variants))
    try:
        with db() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) FROM vantage_simulated_trades "
                f"WHERE status='open' AND tg_source IN ({placeholders})",
                variants,
            ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def get_all_channel_strategy_settings() -> list:
    """Return merged, canonical channel stats for the Channel Strategy UI.

    Aggregates all variant source names (e.g. 'Telegram Auto (Gold Diggers VIP)',
    '1608388054') into the four canonical channels.  The strategy override is
    stored / read under the canonical name only.
    """
    # Merge all rows into canonical buckets (returned even when table has no rows)
    buckets: dict[str, dict] = {
        ch: {"source": ch, "strategy_override": None, "auto_strategy": False,
             "lot_mult": 1.0, "win_rate": 0.0, "sample_n": 0, "net_pnl": 0.0}
        for ch in CANONICAL_CHANNEL_ORDER
    }
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT source, strategy_override, auto_strategy, lot_mult, win_rate, sample_n, net_pnl "
                "FROM channel_performance ORDER BY source"
            ).fetchall()
    except Exception:
        return list(buckets.values())
    for r in rows:
        canon = _canonical(r[0])
        if canon not in buckets:
            continue  # skip manual_market, Manual Signal, etc.
        b = buckets[canon]
        # Strategy override comes from the canonical row only (not variants)
        if r[0] == canon:
            b["strategy_override"] = r[1]
            b["auto_strategy"] = bool(r[2])
        # Aggregate stats
        n_new = int(r[5] or 0)
        n_old = int(b["sample_n"])
        if n_new > 0:
            # Weighted average win rate
            wr_new = float(r[4] or 0)
            if n_old == 0:
                b["win_rate"] = wr_new
            else:
                b["win_rate"] = round(
                    (b["win_rate"] * n_old + wr_new * n_new) / (n_old + n_new), 1
                )
            b["sample_n"] += n_new
        b["net_pnl"] = round(b["net_pnl"] + float(r[6] or 0), 2)

    return [buckets[ch] for ch in CANONICAL_CHANNEL_ORDER]

    # (unreachable fallback)
    return []


def set_channel_paused(source: str, paused: bool) -> None:
    """Manually pause/resume a channel; marks manual_override so auto-recompute
    won't flip it back."""
    import time as _t
    with db() as conn:
        conn.execute(
            "INSERT INTO channel_performance (source, paused, manual_override, updated_at) "
            "VALUES (?,?,1,?) "
            "ON CONFLICT(source) DO UPDATE SET paused=excluded.paused, "
            "  manual_override=1, updated_at=excluded.updated_at",
            (source, 1 if paused else 0, _t.time()),
        )


def get_channel_performance_map() -> dict:
    """Return {source: {lot_mult, paused, manual_override}} for the scorecard UI."""
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT source, lot_mult, paused, manual_override FROM channel_performance"
            ).fetchall()
        return {r[0]: {"lot_mult": float(r[1] or 1.0), "paused": bool(r[2]),
                       "manual_override": bool(r[3])} for r in rows}
    except Exception:
        return {}
