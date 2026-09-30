"""Replay Trend PA over recorded candles. Pure: candle lists in, trades out.

At every closed M15 bar the strategy is handed the bars that had CLOSED by
that moment -- on every timeframe -- and nothing else. A setup opens a
virtual trade at that bar's close and it is resolved on the bars AFTER it,
stop first when one bar spans both (outcome.resolve_bars). One trade at a
time: overlapping entries ride the same move, and a sample of them overstates
how much evidence there is (engines README, 2026-09-24).

Not modelled: the live engine's spread at the moment, slippage, news. `cost`
is charged on every trade instead (default $0.30 on XAUUSD: a typical spread
plus commission).
"""
from __future__ import annotations

import bisect
import re
from typing import Optional

from backend.src.services.trend_pa import outcome as oc
from backend.src.services.trend_pa import strategy as st

# The bridge stamps bars in broker time, UTC+3 -- the same constant
# services/backtest/engine.py and risk/governor.rg_day_start_ts use.
BROKER_OFFSET_S = 10_800

M15_S, H1_S, H4_S = 900, 3600, 14400
DEFAULT_COST = 0.30
DEFAULT_MAX_HOLD_S = 24 * 3600

# How much of each timeframe the strategy is shown per step. Enough for the
# H4 EMA50 to have settled and for five days of H1 levels.
_WINDOW = {"h4": 200, "h1": 150, "m15": 60}


def reason_key(reason: str) -> str:
    """A refusal with its numbers taken out, so refusals can be counted."""
    return re.sub(r"-?\d+(\.\d+)?x", "Nx", reason.split(" (")[0])


def _closed_upto(bars: list, closes: list, now: float, n: int) -> list:
    """The last `n` bars that had closed by `now`."""
    k = bisect.bisect_right(closes, now)
    return bars[max(0, k - n):k]


def run(h4: list, h1: list, m15: list, params: Optional[dict] = None,
        cost: float = DEFAULT_COST, max_hold_s: float = DEFAULT_MAX_HOLD_S,
        reasons: Optional[dict] = None, offset_s: int = BROKER_OFFSET_S) -> list:
    """Every trade the strategy would have taken, oldest first.

    `offset_s` is how far the bars' stamps run ahead of UTC. The engine's own
    replay passes 0: it reads `/candles_range`, which answers in true UTC.
    Until 2026-10-01 it passed nothing, so every session decision in the
    replay was made three hours late (tests/trend_pa/test_sessions_and_clock.py).
    """
    p = {**st.DEFAULTS, **(params or {})}
    h4_close = [float(b["ts"]) + H4_S for b in h4]
    h1_close = [float(b["ts"]) + H1_S for b in h1]
    trades: list = []
    busy_until = float("-inf")

    for i in range(len(m15)):
        now = float(m15[i]["ts"]) + M15_S
        if now < busy_until:
            continue
        w4 = _closed_upto(h4, h4_close, now, _WINDOW["h4"])
        w1 = _closed_upto(h1, h1_close, now, _WINDOW["h1"])
        w15 = m15[max(0, i + 1 - _WINDOW["m15"]):i + 1]
        if not w4 or not w1:
            continue
        setup = st.evaluate(w4, w1, w15, st.broker_ts_to_utc(now, offset_s), p)
        if isinstance(setup, str):
            if reasons is not None:
                key = reason_key(setup)
                reasons[key] = reasons.get(key, 0) + 1
            continue
        res = oc.resolve_bars(setup.direction, setup.entry, setup.stop_loss,
                              setup.take_profit, m15[i + 1:], opened_ts=now,
                              max_hold_s=max_hold_s)
        if res is None:
            break  # still open at the end of the data: not a result
        trades.append({
            "created_at": now, "direction": setup.direction, "pattern": setup.pattern,
            "entry": setup.entry, "stop_loss": setup.stop_loss,
            "take_profit": setup.take_profit, "risk": setup.risk,
            "level": setup.level, "level_kind": setup.level_kind,
            "session": setup.session, "atr_m15": setup.atr_m15,
            "features": setup.features, **res,
            "closed_at": res["exit_ts"],
            "r_net": oc.r_multiple(setup.direction, setup.entry, res["exit_price"],
                                   setup.risk, cost),
        })
        busy_until = float(res["exit_ts"]) + M15_S
    return trades
