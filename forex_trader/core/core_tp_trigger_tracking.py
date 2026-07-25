"""TP/SL trigger detection -- extracted verbatim (no logic changes) from
core/engine.py's SimulationEngine._get_triggered_tps/_last_closed_tp/
_log_tp_wait_diagnostic/_check_sl/_check_tp_hits/_get_remaining_lots, as
part of the core/engine.py migration series. See
docs/todo/refactor/core-tp-trigger-tracking-migration/020-*.md.

_get_triggered_tps and _log_tp_wait_diagnostic originally read/wrote
self._tp_cache/_tp_wait_log_ts -- in-memory state not derivable from the
database (a 2.5s TTL cache and a per-trade log-throttle timestamp).
TPCache carries that state explicitly instead; callers own one TPCache and
pass it into get_triggered_tps/log_tp_wait_diagnostic/check_tp_hits (which
calls get_triggered_tps internally). last_closed_tp/check_sl/
get_remaining_lots never used `self` and extract as plain functions.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from forex_trader.core import database as db_module
from forex_trader.core.models import Tick, MAX_TP

log = logging.getLogger(__name__)

_TP_NUM_RE    = re.compile(r'^TP(\d+)', re.IGNORECASE)
_TP_CACHE_TTL = 2.5  # seconds — in-memory triggered-TP cache TTL (safe for 1s poll rate)
_TP_WAIT_LOG_INTERVAL = 60.0  # seconds between diagnostic log lines per trade


class TPCache:
    """Per-engine in-memory state for the two TP-tracking functions that need it."""
    def __init__(self):
        self.triggered: dict[str, tuple[set, float]] = {}
        self.wait_log_ts: dict[str, float] = {}


async def get_triggered_tps(cache: TPCache, trade_id: str) -> set[int]:
    now = time.time()
    cached = cache.triggered.get(trade_id)
    if cached is not None:
        cached_set, cached_ts = cached
        if now - cached_ts < _TP_CACHE_TTL:
            return cached_set
    def _fetch():
        with db_module.db() as conn:
            return conn.execute(
                "SELECT reason FROM vantage_partial_closes WHERE trade_id=?", (trade_id,)
            ).fetchall()
    rows = await db_module.to_db_thread(_fetch)
    triggered: set[int] = set()
    for row in rows:
        m = _TP_NUM_RE.match(row[0] or "")
        if m:
            triggered.add(int(m.group(1)))
    cache.triggered[trade_id] = (triggered, now)
    return triggered


def last_closed_tp(trade_id: str) -> Optional[int]:
    """Return the TP number of the most recent real partial close (lots > 0), or None."""
    with db_module.db() as conn:
        rows = conn.execute(
            "SELECT reason FROM vantage_partial_closes "
            "WHERE trade_id=? AND lots_closed > 0 "
            "ORDER BY ts DESC LIMIT 10",
            (trade_id,),
        ).fetchall()
    for row in rows:
        m = _TP_NUM_RE.match(row[0] or "")
        if m:
            return int(m.group(1))
    return None


def log_tp_wait_diagnostic(
    cache: TPCache, trade_id: str, tag: str, direction: str,
    current_price: float, target_price: float, hit: bool,
) -> None:
    """Throttled, always-on (INFO, not DEBUG) visibility into the TP1-wait
    comparison every strategy handler runs every poll. One line per trade
    per _TP_WAIT_LOG_INTERVAL, plus always logs the transition to hit=True."""
    now = time.time()
    last = cache.wait_log_ts.get(trade_id, 0.0)
    if not hit and (now - last) < _TP_WAIT_LOG_INTERVAL:
        return
    cache.wait_log_ts[trade_id] = now
    dist = (current_price - target_price) if direction == "BUY" else (target_price - current_price)
    log.info(
        "[%s] %s waiting: price=%.2f target=%.2f dist=%.2f hit=%s",
        tag, trade_id[:8], current_price, target_price, dist, hit,
    )


async def check_tp_hits(cache: TPCache, trade: dict, tick: Tick) -> list[tuple]:
    direction   = trade["direction"].upper()
    entry_price = float(trade["entry_price"])
    already     = await get_triggered_tps(cache, trade["trade_id"])
    hits = []
    for i in range(1, MAX_TP + 1):
        if i in already:
            continue
        tp_val = trade.get(f"tp{i}")
        if tp_val is None:
            continue
        tp_val = float(tp_val)
        # A TP on the wrong side of entry is not a profit target — skip it
        if direction == "BUY"  and tp_val <= entry_price:
            continue
        if direction == "SELL" and tp_val >= entry_price:
            continue
        if direction == "BUY" and tick.bid >= tp_val:
            hits.append((trade["trade_id"], i))
        elif direction == "SELL" and tick.ask <= tp_val:
            hits.append((trade["trade_id"], i))
    return hits


def get_remaining_lots(trade_id: str) -> float:
    """Read current remaining_lots from DB to avoid stale snapshot."""
    with db_module.db() as conn:
        row = conn.execute(
            "SELECT remaining_lots FROM vantage_simulated_trades WHERE trade_id=?",
            (trade_id,)
        ).fetchone()
    return float(row[0]) if row else 0.0
