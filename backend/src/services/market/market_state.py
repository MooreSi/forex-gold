"""The market a signal was born into, in three-state labels. docs/todo/017 item 4.

Before this, a row carried session, H1/H4 bias, ADX and ATR, but no read of
"trending or ranging" (`regime_score` is an ADX bucket, docs/todo/016) and no
volatility label outside an optional ML input. So "how did we do in a ranging,
volatile London morning" could not be asked of either engine's data.

One read, the same for both engines, from H1 bars that had CLOSED before the
signal: a bar still open at signal time is never read, so a stored state can
be backfilled for history without knowing the future.

  * `h1_regime` / `h1_dir` / `h1_er` / `h1_side`: `h1_regime.read` over the
    last `REGIME_HOURS` closed bars at `REGIME_THRESHOLD`, the window and
    threshold Auto templates use (positions/core_auto_template.py, from the
    2026-10-06 study). `h1_side` is with / against the trend, or flat.
  * `vol_ratio` / `vol_state`: ATR(14) on H1 at the last closed bar over the
    median ATR(14) of the `VOL_REF_BARS` bars before it (about five trading
    days). low < `VOL_LOW` <= normal <= `VOL_HIGH` < high.
  * `session`, `hour`, `weekday`: from the signal's UTC time.

Unmeasurable fields are None, never a neutral. At most three states per axis
on purpose: more cells leave too few trades in each to judge.

Not a model input. Pure: bars in, a dict out.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import Optional, Sequence

from backend.src.services.market import h1_regime
from backend.src.utils.sessions import session_for_hour

VERSION = 1
H1_S = 3600.0

REGIME_HOURS = 6
REGIME_THRESHOLD = 0.4

ATR_PERIOD = 14
VOL_REF_BARS = 120
VOL_MIN_REF = 60
VOL_LOW = 0.8
VOL_HIGH = 1.25

# H1 history a caller must fetch before the earliest signal it measures.
LOOKBACK_S = (VOL_REF_BARS + ATR_PERIOD + 4) * H1_S


def _atr_series(bars: Sequence[dict]) -> list[Optional[float]]:
    """Simple-mean ATR(14) at each bar; None until there are 14 true ranges."""
    trs: list[float] = []
    out: list[Optional[float]] = []
    prev_close: Optional[float] = None
    for b in bars:
        hi, lo = float(b["high"]), float(b["low"])
        tr = hi - lo if prev_close is None else max(
            hi - lo, abs(hi - prev_close), abs(lo - prev_close))
        trs.append(tr)
        prev_close = float(b["close"])
        out.append(sum(trs[-ATR_PERIOD:]) / ATR_PERIOD if len(trs) >= ATR_PERIOD else None)
    return out


def _vol(closed: Sequence[dict]) -> tuple[Optional[float], Optional[str]]:
    atr = _atr_series(closed)
    if not atr or atr[-1] is None:
        return None, None
    ref = [a for a in atr[-1 - VOL_REF_BARS:-1] if a]
    if len(ref) < VOL_MIN_REF:
        return None, None
    med = statistics.median(ref)
    if med <= 0:
        return None, None
    ratio = atr[-1] / med
    state = "low" if ratio < VOL_LOW else "high" if ratio > VOL_HIGH else "normal"
    return round(ratio, 4), state


def snapshot(h1_bars: Sequence[dict], t: float, direction: str) -> dict:
    """The state at time `t` for a signal in `direction`."""
    closed = sorted((b for b in h1_bars if float(b["ts"]) + H1_S <= t),
                    key=lambda b: float(b["ts"]))
    r = h1_regime.read(closed, REGIME_HOURS, REGIME_THRESHOLD)
    vol_ratio, vol_state = _vol(closed)
    when = datetime.fromtimestamp(float(t), timezone.utc)
    return {
        "v": VERSION,
        "h1_regime": r.regime if r else None,
        "h1_dir": r.direction if r else None,
        "h1_er": r.er if r else None,
        "h1_side": h1_regime.side(r, direction) if r else None,
        "vol_ratio": vol_ratio,
        "vol_state": vol_state,
        "session": session_for_hour(when.hour),
        "hour": when.hour,
        "weekday": when.weekday(),
    }


def regime_cell(state: dict) -> Optional[str]:
    """trend_with / trend_against / range, or None when unmeasured."""
    regime = (state or {}).get("h1_regime")
    if regime == "range":
        return "range"
    if regime == "trend":
        side = state.get("h1_side")
        return f"trend_{side}" if side in ("with", "against") else None
    return None
