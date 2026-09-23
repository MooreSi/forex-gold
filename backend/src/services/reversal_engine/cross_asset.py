"""Cross-asset context at a signal's creation. docs/todo/reversal-engine/230.

What the markets that move with gold were doing when a signal fired: four
numbers per peer, from closed M5 bars only.

  z15, z60   the peer's 15- and 60-minute return, signed by the trade's
             direction (+ = moved the way a BUY of gold would want if the
             peer moved with gold), in units of the peer's own volatility --
             so silver's 0.3% and the dollar index's 0.05% read on one scale.
  vol_ratio  the last hour's volatility over the last six hours'. Above 1:
             the peer just woke up.
  corr       correlation of 5-minute returns with gold over the last six
             hours. Whether the relationship is holding right now.

**No look-ahead.** Only bars that CLOSED at or before `t` are read. The same
function computes a live signal and a back-filled historical one, which is
what makes the history trainable.

**A missing value is None, never a fabricated 0.** A peer the broker did not
send, one whose last bar is more than 15 minutes old (the bridge's
`copy_rates_from_pos` path served peers up to 113 days stale on
2026-09-23), a flat series or too little history all come back None. The
model reads the documented neutral in its place (`vector`), and the stored
row keeps the None so a reader can tell the two apart.

Pure. Nothing here reaches a broker or decides anything.
"""
from __future__ import annotations

import json
import math
from typing import Optional, Sequence

from backend.src.services.market import correlation as _corr

# Broker symbol names, checked against the demo bridge on 2026-09-23 with M5
# history back to 2026-07-23. EURUSD and NAS100 are served too and left out:
# EURUSD is ~58% of the dollar index, and NAS100 moves with SP500.
PEERS: tuple[str, ...] = ("XAGUSD", "XPTUSD", "USDX", "USDJPY", "SP500", "VIX", "USOUSD")
GOLD = "XAUUSD"

SCHEMA_VERSION = 1
BAR_S = 300
WINDOW = 72          # six hours of M5 returns
SHORT = 3            # 15 minutes
HOUR = 12            # 60 minutes
MAX_AGE_S = 15 * 60  # older than this at `t` and the peer counts as missing
Z_CLAMP = 5.0
VOL_CLAMP = 5.0

_PER_PEER = ("z15", "z60", "vol_ratio", "corr")
FEATURE_NAMES: list[str] = [f"{p}_{k}" for p in PEERS for k in _PER_PEER]
_NEUTRAL = {"z15": 0.0, "z60": 0.0, "vol_ratio": 1.0, "corr": 0.0}

_MISSING = {k: None for k in _PER_PEER}


def _closed_before(candles: Sequence[dict], t: float) -> list[dict]:
    """Bars that closed at or before `t`, each stamped on its 5-minute
    boundary.

    The snap is not cosmetic. The bridge stamps each symbol's bars a few
    seconds off the boundary -- silver +1s, the S&P +9s, VIX +40s on
    2026-09-23 -- while gold's are exact, and the correlation pairs bars by
    timestamp. Unsnapped, VIX lined up with gold on 0 of 6,570 signals.
    """
    out = []
    for c in candles or ():
        try:
            ts = math.floor(float(c.get("ts") or 0.0) / BAR_S) * BAR_S
            close = float(c.get("close") or 0.0)
        except (TypeError, ValueError):
            continue
        if ts + BAR_S <= t and close > 0:
            out.append({"ts": ts, "close": close})
    out.sort(key=lambda c: c["ts"])
    return out


def _std(xs: Sequence[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def peer_features(gold: Sequence[dict], peer: Sequence[dict], t: float,
                  direction: str) -> dict:
    """`{z15, z60, vol_ratio, corr}` for one peer at time `t`, each None when
    it cannot be measured honestly."""
    bars = _closed_before(peer, t)
    if len(bars) < WINDOW + 1:
        return dict(_MISSING)
    if float(bars[-1]["ts"]) + BAR_S < t - MAX_AGE_S:
        return dict(_MISSING)

    bars = bars[-(WINDOW + 1):]
    closes = [float(c["close"]) for c in bars]
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes))]
    sigma = _std(rets)
    if sigma <= 0:
        return dict(_MISSING)

    sign = 1.0 if str(direction).upper() == "BUY" else -1.0

    def _z(k: int) -> float:
        r = closes[-1] / closes[-1 - k] - 1.0
        return max(-Z_CLAMP, min(Z_CLAMP, sign * r / (sigma * math.sqrt(k))))

    vol_ratio = min(VOL_CLAMP, _std(rets[-HOUR:]) / sigma)

    g = _closed_before(gold, t)[-(WINDOW + 1):]
    xs, ys = _corr.align(_corr.returns(g), _corr.returns(bars))
    rho = _corr.pearson(xs, ys) if len(xs) >= WINDOW // 2 else None

    return {"z15": round(_z(SHORT), 4), "z60": round(_z(HOUR), 4),
            "vol_ratio": round(vol_ratio, 4),
            "corr": None if rho is None else round(rho, 4)}


def snapshot(gold: Sequence[dict], peers: dict, t: float, direction: str) -> dict:
    """The stored record for one signal: every peer, missing ones as None."""
    feats: dict = {}
    for p in PEERS:
        f = peer_features(gold, peers.get(p) or [], t, direction)
        for k in _PER_PEER:
            feats[f"{p}_{k}"] = f[k]
    return {"v": SCHEMA_VERSION, "t": t, "features": feats}


def vector(stored) -> Optional[list[float]]:
    """The model's view of a stored record: neutrals for missing values,
    extremes clamped. None for a row that was never measured."""
    if stored is None:
        return None
    if isinstance(stored, str):
        try:
            stored = json.loads(stored)
        except (TypeError, ValueError):
            return None
    if not isinstance(stored, dict):
        return None
    feats = stored.get("features") or {}
    out = []
    for name in FEATURE_NAMES:
        kind = name.rsplit("_", 1)[1] if not name.endswith("vol_ratio") else "vol_ratio"
        v = feats.get(name)
        if v is None:
            out.append(_NEUTRAL[kind])
            continue
        v = float(v)
        if kind in ("z15", "z60"):
            v = max(-Z_CLAMP, min(Z_CLAMP, v))
        elif kind == "vol_ratio":
            v = max(0.0, min(VOL_CLAMP, v))
        out.append(v)
    return out


def daily_correlation(records) -> list[dict]:
    """`[{day, n, <peer>: mean corr | None, ...}]`, oldest first.

    The chart's "is the relationship holding?" line: each peer's six-hour
    correlation with gold, averaged over that UTC day's signals. A peer not
    measured that day is None, never 0 -- zero would claim the two moved
    independently.
    """
    from datetime import datetime, timezone
    days: dict[str, dict] = {}
    for r in records or ():
        try:
            stored = json.loads(r.get("xasset_json") or "")
            feats = stored.get("features") or {}
            day = datetime.fromtimestamp(float(r["created_at"]), timezone.utc).strftime("%Y-%m-%d")
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        d = days.setdefault(day, {"n": 0, "sums": {}, "counts": {}})
        d["n"] += 1
        for p in PEERS:
            v = feats.get(f"{p}_corr")
            if v is None:
                continue
            d["sums"][p] = d["sums"].get(p, 0.0) + float(v)
            d["counts"][p] = d["counts"].get(p, 0) + 1
    out = []
    for day in sorted(days):
        d = days[day]
        row = {"day": day, "n": d["n"]}
        for p in PEERS:
            c = d["counts"].get(p, 0)
            row[p] = round(d["sums"][p] / c, 4) if c else None
        out.append(row)
    return out
