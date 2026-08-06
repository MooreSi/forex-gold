"""Did the professionals' call actually work? (2026-08-06)

Nothing in the app has ever recorded whether a Gold Diggers signal won or
lost -- vantage_tg_signals carries a `status`, which is our processing state,
not their result. So the learning corpus could only ever say "they fired
here", never "they fired here AND it paid".

This module fills that gap by walking M1 candles forward from each captured
signal and judging it on its own stated levels: filled when price trades
into their zone, then win if TP1 is touched before the stop, loss if the
stop goes first.

WHY A CURSOR AND NOT ONE BIG BACKFILL
-------------------------------------
A signal can sit unresolved for hours. Re-reading its whole history every
pass would mean re-fetching the same candles repeatedly for every open row.
Instead each row remembers how far it has been walked (resolve_cursor_ts)
and each pass only advances from there, so the work is proportional to
elapsed time, not to how long the row has been open.

CONSERVATIVE BY CONSTRUCTION
----------------------------
  * Both stop and TP1 inside one M1 candle is scored a LOSS. M1 OHLC does
    not say which came first, and treating an ambiguous bar as a win would
    flatter them exactly where the truth matters most.
  * Never filled within _FILL_WINDOW_S -> 'no_fill', not a loss. They never
    took the trade, so it is not evidence about their judgement.
  * Still open at _MAX_HOLD_S -> 'timeout', scored at the last close, so a
    trade that drifted sideways for half a day cannot masquerade as a win.

Only TP1 is judged. Their later targets are managed by hand in the channel
(partial closes, moved stops, "close it here" messages) and any model of
those would be invention rather than measurement.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from forex_trader.reversal_engine import pro_corpus

log = logging.getLogger(__name__)

_FILL_WINDOW_S = 4 * 3600     # zone must be reached within 4h or they never traded it
_MAX_HOLD_S    = 12 * 3600    # judged at the last close after this
_MAX_RANGE_S   = 6 * 3600     # most candles fetched for one row in one pass


def _candle_ts(c: dict) -> float:
    return float(c.get("time") or c.get("ts") or 0)


def _resolve_row(row: dict, candles: list[dict]) -> Optional[dict]:
    """Advance one snapshot through `candles`. Returns None when it is still
    undecided, else {'outcome', 'r', 'fill'}.

    `candles` must be ordered oldest-first and already trimmed to those the
    row has not seen. Pure: does no I/O, which is what makes the decision
    table directly testable.
    """
    direction = (row.get("direction") or "").upper()
    lo, hi    = float(row["entry_low"]), float(row["entry_high"])
    sl, tp1   = float(row["stop_loss"]), float(row["tp1"])
    signal_ts = float(row.get("signal_ts") or 0)
    fill      = row.get("entry_fill_price")
    is_buy    = direction == "BUY"

    entry_mid = (lo + hi) / 2
    risk = abs(entry_mid - sl)
    if risk <= 0:
        return {"outcome": "invalid", "r": None, "fill": None}

    for c in candles:
        ts   = _candle_ts(c)
        high = float(c.get("high") or 0)
        low  = float(c.get("low") or 0)
        if not (high and low):
            continue

        if fill is None:
            # Filled the moment the bar's range overlaps their zone. Priced at
            # the zone edge they'd have been filled at, not the mid, so a wide
            # zone doesn't quietly hand them a better entry than the market gave.
            if low <= hi and high >= lo:
                fill = hi if is_buy else lo
                # A bar can fill and resolve in one go -- fall through rather
                # than continuing, so a violent entry candle is not ignored.
            elif signal_ts and ts - signal_ts > _FILL_WINDOW_S:
                return {"outcome": "no_fill", "r": None, "fill": None}
            else:
                continue

        risk_f = abs(fill - sl)
        if risk_f <= 0:
            return {"outcome": "invalid", "r": None, "fill": fill}

        hit_sl  = low <= sl if is_buy else high >= sl
        hit_tp1 = high >= tp1 if is_buy else low <= tp1
        if hit_sl:
            # Ambiguous bar (both levels touched) deliberately reads as a loss.
            return {"outcome": "loss", "r": -1.0, "fill": fill}
        if hit_tp1:
            r = abs(tp1 - fill) / risk_f
            return {"outcome": "win", "r": round(r, 4), "fill": fill}

        if signal_ts and ts - signal_ts > _MAX_HOLD_S:
            close = float(c.get("close") or fill)
            r = (close - fill) / risk_f if is_buy else (fill - close) / risk_f
            return {"outcome": "timeout", "r": round(r, 4), "fill": fill}

    return None


async def resolve_pending(bridge: Any, max_rows: int = 25) -> int:
    """Walk every unresolved captured signal forward. Returns how many reached
    a verdict this pass. Never raises -- this is research bookkeeping and must
    not be able to disturb trading."""
    try:
        pending = pro_corpus.unresolved()
    except Exception as exc:
        log.debug("[ProOutcome] cannot read pending rows: %s", exc)
        return 0
    if not pending:
        return 0

    resolved = 0
    now = time.time()
    for row in pending[:max_rows]:
        try:
            start = float(row.get("resolve_cursor_ts") or row.get("signal_ts") or 0)
            if not start:
                continue
            end = min(now, start + _MAX_RANGE_S)
            candles = await bridge.get_candles_range(start, end, "M1")
            if not candles:
                continue
            candles = sorted(candles, key=_candle_ts)
            verdict = _resolve_row(row, candles)
            cursor = _candle_ts(candles[-1]) or end
            if verdict is None:
                pro_corpus.set_cursor(row["id"], cursor, row.get("entry_fill_price"))
                continue
            pro_corpus.set_cursor(row["id"], cursor, verdict.get("fill"))
            pro_corpus.set_outcome(row["id"], verdict["outcome"], verdict.get("r"))
            resolved += 1
            log.info("[ProOutcome] %s %s -> %s (R=%s)", row.get("tg_message_id"),
                     row.get("direction"), verdict["outcome"], verdict.get("r"))
        except Exception as exc:
            log.debug("[ProOutcome] row %s failed: %s", row.get("id"), exc)
    return resolved
