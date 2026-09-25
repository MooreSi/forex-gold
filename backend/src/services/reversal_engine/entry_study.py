"""Does any entry rule, or any fact about the approach, beat no-edge entries?

Phase 1 of `docs/todo/reversal-engine/240`. Pure: bars and signal rows in,
numbers out. No database, no broker, no clock. `tools/re_entry_study.py`
fetches the bars and prints the report.

**The yardstick is a placebo, not a formula.** The chance rate `SL/(SL+TP)`
holds for a continuous path. A replay on M1 bars resolves a bar that spans
both the stop and the target as a loss (`exit_replay` does, on purpose), so
it runs below the formula by an amount that depends on volatility. Comparing
a bar replay with the formula would read that pessimism as "worse than
chance". So every real entry is compared with placebo entries replayed the
SAME way: random bars on the same day, same direction, same stop and target.
Whatever bias the replay has, both sides carry it.

**Nothing here looks ahead.** Features read bars strictly before the entry
bar. The entry bar itself is walked from the entry price only on its adverse
side: its favourable extreme may have printed before the touch, so it is not
credited.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Optional, Sequence

from backend.src.services.market import exit_replay as er

BAR_S = 60.0
# How long a pending signal may wait for its zone. Mirrors
# reversal_engine_service._SIGNAL_MAX_AGE_S (2h).
MAX_WAIT_S = 7200.0
# How long a replayed trade may run before the path ends.
HORIZON_S = 6 * 3600.0


@dataclass(frozen=True)
class Bar:
    ts: float
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Sig:
    id: int
    created_at: float
    direction: str      # BUY | SELL
    entry_low: float
    entry_high: float
    level_price: float
    level_type: str = ""
    session: str = ""


@dataclass(frozen=True)
class Entry:
    idx: int            # index of the entry bar
    price: float
    ts: float


def bars_from(rows) -> list[Bar]:
    """Bridge candle dicts to Bars, oldest first, zero-priced rows dropped."""
    out = []
    for r in rows or ():
        try:
            b = Bar(float(r.get("ts") or r.get("time")), float(r["open"]),
                    float(r["high"]), float(r["low"]), float(r["close"]),
                    float(r.get("volume") or 0.0))
        except (KeyError, TypeError, ValueError):
            continue
        if min(b.open, b.high, b.low, b.close) <= 0:
            continue
        out.append(b)
    out.sort(key=lambda b: b.ts)
    return out


def _sign(direction: str) -> float:
    return 1.0 if str(direction).upper() == "BUY" else -1.0


# ── The touch the live engine trades ────────────────────────────────────────

def find_touch(bars: Sequence[Bar], sig: Sig) -> Optional[Entry]:
    """The first bar that OPENS after the signal was created whose range
    reaches the entry zone, and the price the live poll would have filled.

    A BUY is approached from above, so it fills at the zone's top edge, or at
    the open when the bar opened already inside the zone. SELL mirrors it.
    None when the zone is not reached within MAX_WAIT_S (the signal expired).

    **Not the bar the signal was created in.** Its open, and its range up to
    the creation moment, are prices from before the signal existed. The
    first version of this study used that bar and filled at its open, and
    the cohort it flattered -- signals created while price sat at the level
    -- came out at z = +4.7 and +0.06R, which is the opposite of what the
    engine's own tick-by-tick record shows for the same signals. A fill a
    few seconds late is a cost the study should carry, not a price it may
    borrow from the past.
    """
    is_buy = _sign(sig.direction) > 0
    lo, hi = float(sig.entry_low), float(sig.entry_high)
    for i, b in enumerate(bars):
        if b.ts < sig.created_at:
            continue
        if b.ts > sig.created_at + MAX_WAIT_S:
            return None
        if b.low <= hi and b.high >= lo:
            if is_buy:
                px = b.open if lo <= b.open <= hi else hi
            else:
                px = b.open if lo <= b.open <= hi else lo
            return Entry(i, px, b.ts)
    return None


def path_after(bars: Sequence[Bar], entry: Entry, direction: str,
               horizon_s: float = HORIZON_S) -> list:
    """The `exit_replay` path from the entry onwards. The entry bar is walked
    on its adverse side only: its favourable extreme may predate the fill."""
    is_buy = _sign(direction) > 0
    b0 = bars[entry.idx]
    first = ((b0.ts, entry.price, min(b0.low, entry.price)) if is_buy
             else (b0.ts, max(b0.high, entry.price), entry.price))
    out = [first]
    end = entry.ts + horizon_s
    for b in bars[entry.idx + 1:]:
        if b.ts > end:
            break
        out.append((b.ts, b.high, b.low))
    return out


# ── Outcomes ───────────────────────────────────────────────────────────────

def template_policy(cost_pts: float) -> er.ExitPolicy:
    """The live EA template "30 TP1 SL50 and Trail", as far as one partial
    and a runner can express it: stop 5, 55% at 4, the rest runs to 10;
    breakeven +1 once 5 is reached (its TP2); trail 3 behind from 7, in steps
    of 2. The template's 10% at 5 and 20% at 7 are folded into the runner,
    which makes the runner slightly more exposed than the real ladder."""
    return er.ExitPolicy(
        stop_pts=5.0, target_pts=4.0, target_frac=0.55, runner_target_pts=10.0,
        be_trigger_pts=5.0, be_buffer_pts=1.0,
        trail_activation_pts=7.0, trail_distance_pts=3.0, trail_step_pts=2.0,
        cost_pts=cost_pts)


def barrier_policy(stop_pts: float, target_pts: float,
                   cost_pts: float) -> er.ExitPolicy:
    """A plain stop and target, all out at either."""
    return er.ExitPolicy(stop_pts=stop_pts, target_pts=target_pts,
                         target_frac=1.0, cost_pts=cost_pts)


def replay(bars: Sequence[Bar], entry: Entry, direction: str,
           policy: er.ExitPolicy) -> Optional[er.ExitResult]:
    return er.replay(path_after(bars, entry, direction), entry.price,
                     direction, policy)


def won(res: Optional[er.ExitResult]) -> Optional[bool]:
    """A barrier trade won if it reached the target. Path end is neither."""
    if res is None or res.reason in ("path_end", "time"):
        return None
    return res.reason == "target"


# ── Placebo: the same replay from bars that were not chosen ────────────────

def placebo_rate(bars: Sequence[Bar], entry: Entry, direction: str,
                 policy: er.ExitPolicy, rng: random.Random,
                 n: int = 20, window_s: float = 12 * 3600.0) -> Optional[float]:
    """Win rate of `n` entries at random bars within `window_s` of the real
    one, same direction and policy, each filled at its bar's open.

    Random bars in the SAME stretch of tape carry the same volatility, the
    same session mix and the same replay pessimism as the real entry, which
    is what makes the comparison fair. None when fewer than half resolve.
    """
    lo_ts, hi_ts = entry.ts - window_s, entry.ts + window_s
    pool = [i for i, b in enumerate(bars)
            if lo_ts <= b.ts <= hi_ts and i != entry.idx
            and b.ts + HORIZON_S <= bars[-1].ts]
    if not pool:
        return None
    wins = total = 0
    for i in rng.sample(pool, min(n, len(pool))):
        e = Entry(i, bars[i].open, bars[i].ts)
        w = won(replay(bars, e, direction, policy))
        if w is None:
            continue
        total += 1
        wins += 1 if w else 0
    if total < n / 2:
        return None
    return wins / total


# ── Facts about the approach, from bars strictly before the entry ──────────

def _mean_range(bars: Sequence[Bar]) -> float:
    return sum(b.high - b.low for b in bars) / len(bars) if bars else 0.0


def features(bars: Sequence[Bar], entry: Entry, sig: Sig) -> Optional[dict]:
    """Pre-entry facts, or None without 24h of history before the entry.

    `approach_*` is signed so that POSITIVE means price was travelling
    toward the level (falling into a BUY, rising into a SELL).
    """
    i = entry.idx
    day_ago = entry.ts - 86_400.0
    hist = [b for b in bars[:i] if b.ts >= day_ago]
    if len(hist) < 240:
        return None
    unit = _mean_range(hist[-60:])
    if unit <= 0:
        return None
    s = _sign(sig.direction)
    last = hist[-1].close

    def approach(n: int) -> float:
        return (hist[-n - 1].close - last) * s / unit

    vol60 = sum(b.volume for b in hist[-60:]) / 60.0
    vol5 = sum(b.volume for b in hist[-5:]) / 5.0

    # Separate visits to the level in the prior day: a bar spanning it,
    # at least 15 bars after the previous spanning bar.
    level = float(sig.level_price)
    touches, last_touch_ts, prev_idx = 0, None, -10_000
    for k, b in enumerate(hist):
        if b.low <= level <= b.high:
            if k - prev_idx >= 15:
                touches += 1
            prev_idx = k
            last_touch_ts = b.ts
    mins_since = ((entry.ts - last_touch_ts) / 60.0) if last_touch_ts else 1440.0

    day_hi = max(b.high for b in hist[-240:])
    day_lo = min(b.low for b in hist[-240:])
    mean60 = sum(b.close for b in hist[-60:]) / 60.0

    return {
        "approach_5": approach(5),
        "approach_15": approach(15),
        "approach_60": approach(60),
        "range_ratio_3": _mean_range(hist[-3:]) / unit,
        "volume_ratio_5": (vol5 / vol60) if vol60 > 0 else 1.0,
        "touches_24h": float(touches),
        "mins_since_touch": min(mins_since, 1440.0),
        "range_4h": (day_hi - day_lo) / unit,
        "stretch_60": (mean60 - level) * s / unit,
        "hour": float(int((entry.ts % 86_400) // 3600)),
    }


# ── Confirmation entries ───────────────────────────────────────────────────

def find_confirmation(bars: Sequence[Bar], touch: Entry, sig: Sig,
                      within: int = 5, buffer_pts: float = 0.5,
                      min_stop: float = 1.5, max_stop: float = 8.0
                      ) -> Optional[tuple[Entry, float]]:
    """After the touch, the first bar within `within` bars that traded
    THROUGH the level and closed back on the trade's side of it. Entry at
    that close; stop beyond the extreme since the touch plus `buffer_pts`.

    Returns (entry, stop_distance) or None. A stop outside
    [min_stop, max_stop] is no trade: too tight is noise, too wide is not
    the trade the template sizes for.
    """
    is_buy = _sign(sig.direction) > 0
    level = float(sig.level_price)
    extreme = None
    for j in range(touch.idx, min(len(bars), touch.idx + within + 1)):
        b = bars[j]
        extreme = (b.low if extreme is None else min(extreme, b.low)) if is_buy \
            else (b.high if extreme is None else max(extreme, b.high))
        pierced = (extreme < level) if is_buy else (extreme > level)
        back = (b.close > level) if is_buy else (b.close < level)
        if pierced and back:
            stop = (b.close - extreme + buffer_pts) if is_buy \
                else (extreme - b.close + buffer_pts)
            if not (min_stop <= stop <= max_stop):
                return None
            # Enter at the NEXT bar's open: the close is only known once the
            # bar has finished.
            if j + 1 >= len(bars):
                return None
            nb = bars[j + 1]
            stop_adj = stop + ((nb.open - b.close) if is_buy else (b.close - nb.open))
            if not (min_stop <= stop_adj <= max_stop):
                return None
            return Entry(j + 1, nb.open, nb.ts), stop_adj
    return None


# ── Statistics ─────────────────────────────────────────────────────────────

def beats_placebo(wins: Sequence[bool], placebo: Sequence[float],
                  clusters: Optional[Sequence] = None) -> dict:
    """Wins against the sum of each trade's own placebo rate, as a z-score.

    **Trades that overlap in time are not independent.** Two entries ten
    minutes apart mostly ride the same move, so they win or lose together,
    and `sqrt(Σp(1-p))` treats them as two coins. It then overstates the
    evidence: on a driftless walk with an entry every 14 bars the naive z
    came out at 3.15 (test_entry_study's negative control). The engine fires
    ~150 overlapping signals a day, so this is not a corner case.

    With `clusters` (one key per trade, e.g. the entry's 2-hour block) the
    variance is the cluster-robust one, `Σ_c (Σ_{i in c} (w_i - p_i))²`,
    which lets trades in the same cluster move together. Without it, each
    trade is its own cluster.
    """
    n = len(wins)
    if n == 0 or len(placebo) != n or (clusters is not None and len(clusters) != n):
        return {"n": n, "win_rate": None, "placebo": None, "z": None}
    w = sum(1 for x in wins if x)
    ep = sum(placebo)
    keys = clusters if clusters is not None else range(n)
    resid: dict = {}
    for k, x, p in zip(keys, wins, placebo):
        resid[k] = resid.get(k, 0.0) + ((1.0 if x else 0.0) - p)
    var = sum(r * r for r in resid.values())
    z = (w - ep) / math.sqrt(var) if var > 0 else None
    return {"n": n, "win_rate": w / n, "placebo": ep / n,
            "clusters": len(resid), "z": round(z, 2) if z is not None else None}


def quantile_edges(values: Sequence[float], k: int = 5) -> list[float]:
    """The k-1 interior cut points of `values`."""
    xs = sorted(values)
    if not xs:
        return []
    return [xs[min(len(xs) - 1, int(len(xs) * q / k))] for q in range(1, k)]


def bucket(x: float, edges: Sequence[float]) -> int:
    for i, e in enumerate(edges):
        if x < e:
            return i
    return len(edges)
