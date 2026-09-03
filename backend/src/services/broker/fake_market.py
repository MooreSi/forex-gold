"""Deterministic synthetic XAUUSD market for debug mode.

The whole price history is a closed-form function of the absolute unix
timestamp, so `get_tick_at(ts)`, candles and the live tick all answer from
the same curve and any two instances with the same seed agree exactly —
no stored state, no wall-clock dependence beyond the timestamp you ask
about.

Default stream: a slow and a fast sine plus a small seeded-LCG jitter
around a base price. Scenario mode: piecewise-linear interpolation
between `[seconds_from_base, mid]` anchor points (JSON-friendly — see
tools/debug_scenarios/), holding the last anchor afterwards, so a test or
demo can force a TP/SL touch at a chosen second.

No network code lives here or in fake_bridge.py.
"""
from __future__ import annotations

import math
from typing import Optional

from backend.src.utils.models import DIGITS, POINT_SIZE, Tick

DEFAULT_START_PRICE = 2400.0
DEFAULT_SPREAD = 0.30


def _lcg_unit(n: int, seed: int) -> float:
    """Seeded pseudo-random in [-1, 1], pure function of (n, seed)."""
    state = (1103515245 * (n ^ (seed * 2654435761)) + 12345) & 0x7FFFFFFF
    state = (1103515245 * state + 12345) & 0x7FFFFFFF
    return (state / 0x3FFFFFFF) - 1.0


class FakeMarket:
    def __init__(
        self,
        seed: int = 42,
        start_price: float = DEFAULT_START_PRICE,
        spread: float = DEFAULT_SPREAD,
        base_ts: float = 0.0,
        scenario: Optional[dict] = None,
    ):
        self.seed = seed
        self.start_price = float(start_price)
        self.spread = float(spread)
        self.base_ts = float(base_ts)
        anchors = (scenario or {}).get("anchors") or []
        # [(seconds_from_base, mid)] sorted; empty → synthetic default stream
        self.anchors: list[tuple[float, float]] = sorted(
            (float(t), float(m)) for t, m in anchors
        )

    # ── The curve ─────────────────────────────────────────────────────────

    def mid(self, ts: float) -> float:
        s = ts - self.base_ts
        if self.anchors:
            return self._scripted_mid(s)
        return (
            self.start_price
            + 5.0 * math.sin(s / 120.0)
            + 1.5 * math.sin(s / 17.0)
            + 0.4 * _lcg_unit(int(s), self.seed)
        )

    def _scripted_mid(self, s: float) -> float:
        anchors = self.anchors
        if s <= anchors[0][0]:
            return anchors[0][1]
        for (t0, m0), (t1, m1) in zip(anchors, anchors[1:]):
            if s <= t1:
                frac = (s - t0) / (t1 - t0) if t1 > t0 else 1.0
                return m0 + (m1 - m0) * frac
        return anchors[-1][1]

    # ── Views over the curve ──────────────────────────────────────────────

    def tick(self, ts: float) -> Tick:
        mid = self.mid(ts)
        bid = round(mid - self.spread / 2.0, DIGITS)
        ask = round(mid + self.spread / 2.0, DIGITS)
        spread = round(ask - bid, 5)
        return Tick(
            bid=bid,
            ask=ask,
            mid=round((bid + ask) / 2.0, DIGITS),
            spread=spread,
            spread_points=round(spread / POINT_SIZE, 1),
            timestamp=ts,
            source="fake",
        )

    def candle(self, bar_start: float, tf_seconds: int) -> dict:
        """OHLC sampled from the curve at ≤10s intervals inside the bar."""
        step = max(1, min(10, tf_seconds // 6))
        samples = [
            self.mid(bar_start + off) for off in range(0, tf_seconds + 1, step)
        ]
        if (tf_seconds % step) != 0:
            samples.append(self.mid(bar_start + tf_seconds))
        return {
            "ts": int(bar_start),
            "open": round(self.mid(bar_start), DIGITS),
            "high": round(max(samples), DIGITS),
            "low": round(min(samples), DIGITS),
            "close": round(self.mid(bar_start + tf_seconds), DIGITS),
            "volume": 100.0,
        }

    def candles(self, end_ts: float, tf_seconds: int, count: int) -> list[dict]:
        """`count` completed bars, oldest first, the newest ending at the bar
        boundary at-or-before end_ts."""
        last_end = math.floor(end_ts / tf_seconds) * tf_seconds
        return [
            self.candle(last_end - tf_seconds * k, tf_seconds)
            for k in range(count, 0, -1)
        ]

    def ticks(self, from_ts: float, to_ts: float, interval: float = 1.0) -> list[dict]:
        """One synthetic tick per `interval` seconds across [from_ts, to_ts)
        -- deterministic, same curve as tick()/candles(), for tests of
        anything that walks real ticks (docs/todo/backtest/010 phase 1)."""
        out = []
        ts = float(from_ts)
        while ts < to_ts:
            t = self.tick(ts)
            out.append({"time": ts, "bid": t.bid, "ask": t.ask})
            ts += interval
        return out


TF_SECONDS = {
    "M1": 60, "M5": 300, "M15": 900, "M30": 1800,
    "H1": 3600, "H4": 14400, "D1": 86400,
}
