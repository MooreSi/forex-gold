"""Max TP hit checker + one-off backfill -- extracted verbatim (no logic
changes) from core/engine.py's SimulationEngine._max_tp_checker_loop's
per-cycle sweep body, _backfill_max_tp_hit_corrected, and the module-level
pure helper _tp_level_from_extreme, as part of the core/engine.py migration
series. See docs/todo/refactor/core-max-tp-hit-migration/020-*.md.

Read-only candle fetches plus DB writes to max_tp_hit -- no order is ever
placed, closed, or modified.

The `_max_tp_checker_loop` while/sleep(90)/sleep(300) shell stays in
engine.py (same split as _tp_safety_net_loop/tp_safety_net_sweep);
`max_tp_checker_sweep` here is just its per-cycle body. `backfill_max_tp_hit_corrected`
has no loop wrapper in the original, so it's ported in full, including its
own startup delay.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from forex_trader.core import database as db_module

log = logging.getLogger(__name__)


def _tp_level_from_extreme(direction: str, extreme: float, tp_source_fn) -> str:
    """Highest TP level (TP1..TP8) crossed by `extreme` in the trade's
    favourable direction. `tp_source_fn(i)` returns the TP price for level i
    (1-indexed) or None. Continues past a None mid-sequence rather than
    stopping, since a gap (e.g. tp2 NULL but tp3-tp8 populated) must not hide
    every level beyond it."""
    hit = "none"
    for i in range(1, 9):
        tp = tp_source_fn(i)
        if tp is None:
            continue
        tp = float(tp)
        if direction == "BUY" and extreme >= tp:
            hit = f"TP{i}"
        elif direction == "SELL" and extreme <= tp:
            hit = f"TP{i}"
    return hit


async def max_tp_checker_sweep(bridge: Any) -> None:
    cutoff = time.time() - 1800  # 30 minutes ago
    pending = db_module.get_trades_pending_max_tp(cutoff)
    for t in pending:
        try:
            from_ts = float(t.get("open_time") or 0)
            to_ts = float(t.get("close_time") or 0)
            direction = (t.get("direction") or "").upper()
            if not from_ts or not to_ts or not direction:
                db_module.save_max_tp_hit(t["trade_id"], "none")
                continue

            candles = await bridge.get_candles_range(from_ts, to_ts, "M1")
            if not candles:
                # Bridge offline or no data — skip, will retry next cycle
                continue

            if direction == "BUY":
                extreme = max(float(c.get("high", 0) or 0) for c in candles)
            else:
                extreme = min(float(c.get("low", float("inf")) or float("inf")) for c in candles)

            def _tp_source(idx: int, t=t):
                sig_v = t.get(f"sig_tp{idx}")
                return sig_v if sig_v is not None else t.get(f"tp{idx}")

            hit = _tp_level_from_extreme(direction, extreme, _tp_source)

            db_module.save_max_tp_hit(t["trade_id"], hit)
            log.info(
                "max_tp_hit trade=%s dir=%s extreme=%.2f -> %s",
                t["trade_id"], direction, extreme, hit,
            )

            try:
                from backend.src.controllers.sync.ledger import push_trade_closed
                net_pnl = t.get("net_pnl")
                push_trade_closed({
                    "trade_id": t["trade_id"],
                    "engine": "main",
                    "direction": direction,
                    "strategy": t.get("strategy", ""),
                    "open_time": t.get("open_time"),
                    "close_time": t.get("close_time"),
                    "pnl_dollars": net_pnl,
                    "outcome": (
                        "win" if (net_pnl or 0) > 0.5
                        else ("loss" if (net_pnl or 0) < -0.5 else "be")
                    ),
                    "tg_source": t.get("tg_source"),
                    "mt5_ticket": t.get("mt5_ticket"),
                    "max_tp_hit": hit,
                })
            except Exception as _le:
                log.debug("[Ledger] max_tp_hit push failed: %s", _le)
        except Exception as _te:
            log.debug("max_tp_hit trade=%s error: %s", t.get("trade_id"), _te)


async def backfill_max_tp_hit_corrected(bridge: Any) -> None:
    """One-off (2026-07-18): recompute max_tp_hit for every already-closed
    trade using the corrected open_time->close_time window, replacing
    values computed under the old close_time+30min window that could
    attribute post-close continuation moves to a trade that was already
    flat (see max_tp_checker_sweep docstring). Safe to leave running at
    startup — recompute-and-compare is idempotent, and it no-ops once
    every row already matches its corrected value."""
    await asyncio.sleep(120)  # let the bridge connect first
    try:
        trades = await db_module.to_db_thread(db_module.get_trades_with_max_tp_set)
    except Exception as e:
        log.warning("[MaxTP-backfill] fetch failed: %s", e)
        return
    corrected = 0
    for t in trades:
        try:
            direction = (t.get("direction") or "").upper()
            from_ts = float(t.get("open_time") or 0)
            to_ts = float(t.get("close_time") or 0)
            if not direction or not from_ts or not to_ts:
                continue
            candles = await bridge.get_candles_range(from_ts, to_ts, "M1")
            if not candles:
                continue
            if direction == "BUY":
                extreme = max(float(c.get("high", 0) or 0) for c in candles)
            else:
                extreme = min(float(c.get("low", float("inf")) or float("inf")) for c in candles)

            def _tp_source(idx: int, t=t):
                sig_v = t.get(f"sig_tp{idx}")
                return sig_v if sig_v is not None else t.get(f"tp{idx}")

            hit = _tp_level_from_extreme(direction, extreme, _tp_source)
            if hit == t.get("old_hit"):
                continue

            await db_module.to_db_thread(db_module.save_max_tp_hit, t["trade_id"], hit)
            corrected += 1
            log.info("[MaxTP-backfill] trade=%s %s -> %s", t["trade_id"], t.get("old_hit"), hit)

            try:
                from backend.src.controllers.sync.ledger import push_trade_closed
                net_pnl = t.get("net_pnl")
                push_trade_closed({
                    "trade_id": t["trade_id"],
                    "engine": "main",
                    "direction": direction,
                    "strategy": t.get("strategy", ""),
                    "open_time": t.get("open_time"),
                    "close_time": t.get("close_time"),
                    "pnl_dollars": net_pnl,
                    "outcome": (
                        "win" if (net_pnl or 0) > 0.5
                        else ("loss" if (net_pnl or 0) < -0.5 else "be")
                    ),
                    "tg_source": t.get("tg_source"),
                    "mt5_ticket": t.get("mt5_ticket"),
                    "max_tp_hit": hit,
                })
            except Exception as _le:
                log.debug("[MaxTP-backfill] ledger push failed trade=%s: %s", t["trade_id"], _le)
        except Exception as _te:
            log.debug("[MaxTP-backfill] trade=%s error: %s", t.get("trade_id"), _te)
    log.info("[MaxTP-backfill] done: %d/%d trades corrected", corrected, len(trades))
