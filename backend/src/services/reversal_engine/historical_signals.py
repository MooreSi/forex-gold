"""Regenerate Reversal Engine signals from M1 bars alone.

docs/todo/reversal-engine/260. Pure: bars in, signals out, no clock, no
database, no broker. It calls the live `level_detector` and
`signal_generator.entry_zone_from_level`, so a level, its score, its
direction and its zone mean exactly what they mean in `_run_cycle`.

WHAT IT REPRODUCES
------------------
Once per cycle, with the candles the live loop fetches (H1 x50, M15 x80,
H4 x6) built from bars strictly BEFORE the cycle time: the ATR gate (2 to
80), `get_htf_bias`, `get_candidate_levels`, the score floor (0.5), the bias
override (a bullish bias refuses a SELL below 0.75, and the reverse), the
30-minute level cooldown, and "the first eligible candidate wins" -- which is
what `rank_eligible_candidates` returns when no model has an opinion.

WHAT IT DOES NOT (the list is in the spec, 260, and printed by the tool)
-----------------------------------------------------------------------
ML and meta-label gates, news and spread gates, the Claude review, the
consecutive-loss cooldown, the six-open-signal cap, liquidity-map levels,
cross-engine conflicts, session toggles. The result is the level-touch family
the engine is built on, not a replay of what it traded on any given day.

NO LOOKAHEAD
------------
A candle that is still forming at the cycle time is built from the M1 bars
that exist by then, as the bridge's forming candle would be. A complete
candle is only used once its whole bucket has ended. Pinned by two tests that
change the future and require the past not to move.
"""
from __future__ import annotations

import bisect
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from backend.src.services.reversal_engine import entry_study as es
from backend.src.services.reversal_engine import historical_bars as hb
from backend.src.services.reversal_engine import level_detector as ld
from backend.src.services.reversal_engine import signal_generator as sg

# What the live loop asks the bridge for.
_H1_N, _M15_N, _H4_N = 50, 80, 6
_ATR_MIN, _ATR_MAX = 2.0, 80.0
_SCORE_FLOOR = 0.50
_BIAS_OVERRIDE_SCORE = 0.75
_COOLDOWN_REACH_PTS = 3.0
# A cycle only runs while the market is feeding bars.
_MAX_BAR_AGE_S = 300.0


@dataclass(frozen=True)
class Generated:
    sig: es.Sig
    score: float
    atr: float
    htf: str


def _candle(b: es.Bar) -> dict:
    # Both spellings: level_detector reads "time", ict_patterns reads "ts".
    return {"ts": b.ts, "time": b.ts, "open": b.open, "high": b.high, "low": b.low,
            "close": b.close, "volume": b.volume}


def _calc_atr(candles: list[dict], period: int = 14) -> float:
    """Mirror of `ReversalEngine._calc_atr` (last `period` true ranges).
    Kept equal by tests/reversal_engine/test_historical_replay.py."""
    if not candles or len(candles) < 2:
        return 8.0
    trs = []
    for i in range(max(1, len(candles) - period), len(candles)):
        h, l, pc = candles[i]["high"], candles[i]["low"], candles[i - 1]["close"]
        if h > 0 and l > 0:
            trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return round(sum(trs) / len(trs), 2) if trs else 8.0


class _Frames:
    """One timeframe's complete buckets, plus the forming one on demand."""

    def __init__(self, m1: Sequence[es.Bar], m1_ts: list[float], seconds: int):
        self.m1, self.m1_ts, self.s = m1, m1_ts, seconds
        self.bars = hb.aggregate(m1, seconds)
        self.ends = [b.ts + seconds for b in self.bars]

    def at(self, t: float, n: int) -> list[dict]:
        """The last `n` candles as seen at `t`: complete buckets that ended by
        `t`, then the bucket still forming, built from bars before `t`."""
        k = bisect.bisect_right(self.ends, t)
        start = (t // self.s) * self.s
        i, j = bisect.bisect_left(self.m1_ts, start), bisect.bisect_left(self.m1_ts, t)
        forming = hb.aggregate(self.m1[i:j], self.s)[-1:] if j > i else []
        done = [_candle(b) for b in self.bars[max(0, k - n): k]]
        out = done + [_candle(b) for b in forming]
        return out[-n:]


def regenerate(m1: Sequence[es.Bar], cycle_s: int = 300,
               cooldown_s: float = 1800.0) -> list[Generated]:
    """Signals the level-touch engine would have raised on these bars."""
    if not m1:
        return []
    m1_ts = [b.ts for b in m1]
    h1, m15, h4 = (_Frames(m1, m1_ts, s) for s in (3600, 900, 14400))
    out: list[Generated] = []
    recent: deque = deque()          # (created_at, direction, level_price)
    first = (m1_ts[0] // cycle_s + 1) * cycle_s + _H1_N * 3600
    t = float(first)
    last = m1_ts[-1] + 60.0
    next_id = 1
    while t <= last:
        cur = t
        t += cycle_s
        i = bisect.bisect_left(m1_ts, cur)
        if i == 0 or cur - (m1_ts[i - 1] + 60.0) > _MAX_BAR_AGE_S:
            continue
        price = m1[i - 1].close
        m15_c = m15.at(cur, _M15_N)
        atr = _calc_atr(m15_c)
        if atr < _ATR_MIN or atr > _ATR_MAX:
            continue
        h1_c, h4_c = h1.at(cur, _H1_N), h4.at(cur, _H4_N)
        htf = ld.get_htf_bias(h1_c, h4_c)
        cands = ld.get_candidate_levels(h1_c, price, htf_bias=htf, m15_candles=m15_c)
        while recent and recent[0][0] <= cur - cooldown_s:
            recent.popleft()
        for lv in cands:
            score, direction = float(lv["score"]), lv["direction"]
            if score < _SCORE_FLOOR:
                continue
            if htf == "bullish" and direction == "SELL" and score < _BIAS_OVERRIDE_SCORE:
                continue
            if htf == "bearish" and direction == "BUY" and score < _BIAS_OVERRIDE_SCORE:
                continue
            lp = float(lv["price"])
            if any(d == direction and abs(p - lp) <= _COOLDOWN_REACH_PTS
                   for _, d, p in recent):
                continue
            if lv.get("profile") == "gd2":
                lo = float(lv.get("entry_zone_low", lp))
                hi = float(lv.get("entry_zone_high", lp))
            else:
                lo, hi = sg.entry_zone_from_level(lp, atr, direction)
            hour = datetime.fromtimestamp(cur, timezone.utc).hour
            sig = es.Sig(next_id, cur, direction, lo, hi, lp,
                         lv.get("type", "unknown"), ld.get_session(hour))
            next_id += 1
            recent.append((cur, direction, lp))
            out.append(Generated(sig, score, atr, htf))
            break
    return out
