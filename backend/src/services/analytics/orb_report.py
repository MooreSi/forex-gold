"""ORB/IVB (pre-London-range breakout) report: range detection, volume
profile, and the backtested target multiple.

The read-only half of what used to be core/core_orb_report.py. The other half,
orb_auto_execute(), places a genuine EA pending order and therefore stays out of
analytics/ -- it moves with the trading surface in phase 8. The split is safe
because the two halves share nothing: auto-execute takes the finished report as
an argument and never calls back into this module.

One deliberate exception to the analytics no-writes rule, so nobody "fixes" it:
get_orb_target_multiple memoises its backtested multiple into app_config
(orb_target_multiple / _n / _date) so the 25-day backtest runs once per day
instead of once per report. That is a self-cache of a computed statistic, not
trading state -- deleting those keys costs one recomputation and nothing else.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.src.db import database as db_module

log = logging.getLogger(__name__)

_ORB_BUCKETS = 40           # volume-profile price buckets across the reference range
_ORB_VALUE_AREA_PCT = 0.70  # standard 70% value-area convention
_ORB_SL_RANGE_PCT = 0.50
_ORB_MIN_ENTRY_STOP_BUFFER_PCT = 0.30

_ORB_BACKTEST_DAYS = 25
_ORB_BACKTEST_HORIZON_HOURS = 10   # rest of London + all of NY session
_ORB_DEFAULT_TARGET_MULTIPLE = 2.0  # standard ORB convention, used until enough real history exists
_ORB_MIN_SAMPLES = 8

_ORB_RANGE_HOURS = 1     # reference range = the last N hours before London open
_ORB_WINDOW_MINUTES = 15  # breakout must be evaluated within N minutes of London open


def _compute_volume_profile(
    candles: list[dict], range_low: float, range_high: float,
    n_buckets: int = _ORB_BUCKETS, value_area_pct: float = _ORB_VALUE_AREA_PCT,
) -> tuple[float, float, float]:
    """
    Approximate volume profile from OHLCV bars (no raw tick data available):
    each candle's tick-volume is distributed evenly across the price buckets
    its high-low range touches. Returns (POC, VAH, VAL) — POC is the highest-
    volume bucket's midpoint; VAH/VAL bound the smallest contiguous band
    around POC containing value_area_pct of total volume (the standard
    market-profile convention), expanding to whichever neighbouring bucket
    has more volume at each step.
    """
    bucket_size = max((range_high - range_low) / n_buckets, 0.01)
    buckets = [0.0] * n_buckets

    def _idx(price: float) -> int:
        return max(0, min(int((price - range_low) / bucket_size), n_buckets - 1))

    for c in candles:
        vol = float(c.get("volume", 0) or 0)
        if vol <= 0:
            continue
        i0, i1 = _idx(c["low"]), _idx(c["high"])
        span = max(i1 - i0 + 1, 1)
        share = vol / span
        for i in range(i0, i1 + 1):
            buckets[i] += share

    poc_idx = max(range(n_buckets), key=lambda i: buckets[i])
    poc = range_low + (poc_idx + 0.5) * bucket_size

    total = sum(buckets) or 1.0
    target = total * value_area_pct
    lo_i = hi_i = poc_idx
    covered = buckets[poc_idx]
    while covered < target and (lo_i > 0 or hi_i < n_buckets - 1):
        left_val = buckets[lo_i - 1] if lo_i > 0 else -1.0
        right_val = buckets[hi_i + 1] if hi_i < n_buckets - 1 else -1.0
        if right_val >= left_val:
            hi_i += 1
            covered += buckets[hi_i]
        else:
            lo_i -= 1
            covered += buckets[lo_i]

    val = range_low + lo_i * bucket_size
    vah = range_low + (hi_i + 1) * bucket_size
    return round(poc, 2), round(vah, 2), round(val, 2)


async def build_orb_report(bridge: Any) -> Optional[dict]:
    """
    Reference range is the last _ORB_RANGE_HOURS (1h) of the Asian
    session immediately before London opens, not the full Asian session
    (00:00-08:00 UTC) this report used until 2026-07-22, and not a
    freshly-forming first-hour-of-London range (used until 2026-07-17,
    before that). Standard London-breakout convention trades the breakout
    of the ALREADY-ESTABLISHED pre-London range the moment London opens.

    The breakout itself is only evaluated inside a bounded
    _ORB_WINDOW_MINUTES-wide (15min) window right after London opens —
    the setup goes stale outside that window (both bounds enforced below),
    rather than staying "live" indefinitely at whatever price happens to
    be current whenever this is next called (e.g. hours later on a manual
    Refresh click).

    Why the earlier full-8h-Asian-range version changed again: it worked
    (see the history below) but the last-1h window is deliberately
    tighter — a London-open breakout is trading the most recent
    consolidation immediately preceding the open, not the whole overnight
    range, which can include unrelated Asian-session moves hours earlier.

    Prior history (why not first-hour-of-London): the Asian range
    averaged ~4x wider than the old London-hour range (~48pt vs ~13pt
    over a 14-day sample, 2026-07-15). Building a volume-profile stop
    from an already-13pt window left nowhere for VAL/VAH to spread —
    confirmed live: all 4 real orb_fixed trades to date hit SL, 3 of
    them with a stop under 1pt on gold (smaller than typical spread).
    Switching to a pre-London reference range, and deriving the stop
    from a fixed fraction of ITS height (_ORB_SL_RANGE_PCT) instead of
    an inner volume-profile boundary, fixed both: the range itself is
    more stable, and the stop no longer depends on how tightly volume
    happens to cluster within it.
    """
    from zoneinfo import ZoneInfo

    tick = await bridge.get_tick()
    if tick is None:
        return None

    now_utc = datetime.now(timezone.utc)
    london_now = now_utc.astimezone(ZoneInfo("Europe/London"))
    london_open = london_now.replace(hour=8, minute=0, second=0, microsecond=0)
    window_start = london_open.astimezone(timezone.utc).timestamp()
    window_end = window_start + _ORB_WINDOW_MINUTES * 60
    now_ts = now_utc.timestamp()

    if now_ts < window_start:
        return None  # London hasn't opened yet this cycle
    if now_ts >= window_end:
        return None  # first 15 minutes of London have already passed — setup is stale

    asia_end = window_start
    asia_start = asia_end - _ORB_RANGE_HOURS * 3600
    asia_candles = await bridge.get_candles_range(asia_start, asia_end, timeframe="M1")
    if not asia_candles:
        return None

    range_high = max(c["high"] for c in asia_candles)
    range_low  = min(c["low"] for c in asia_candles)
    range_height = range_high - range_low
    if range_height <= 0:
        return None

    poc, vah, val = _compute_volume_profile(
        asia_candles, range_low, range_high,
        n_buckets=_ORB_BUCKETS, value_area_pct=_ORB_VALUE_AREA_PCT,
    )

    current_price = float(tick.ask)
    if current_price > range_high:
        direction = "bullish"
    elif current_price < range_low:
        direction = "bearish"
    else:
        direction = "inside"

    report: dict = {
        "current_price": current_price,
        "window_start":  asia_start,
        "window_end":    asia_end,
        "range_high":    range_high,
        "range_low":     range_low,
        "range_height":  range_height,
        "poc":           poc,
        "vah":           vah,
        "val":           val,
        "direction":     direction,
        "candles":       asia_candles,
        "asia_high":     range_high,
        "asia_low":      range_low,
        "asia_range":    range_height,
    }

    if direction == "inside":
        report.update({
            "entry_zone_low": None, "entry_zone_high": None,
            "stop": None, "target": None, "rr": None,
            "position_note": (
                f"still inside the pre-London range — {current_price - range_low:.1f} pts "
                f"above the Low, {range_high - current_price:.1f} pts below the High"
            ),
        })
        return report

    target_info = await get_orb_target_multiple(bridge)
    breakout_edge = range_high if direction == "bullish" else range_low
    target = (
        breakout_edge + target_info["multiple"] * range_height if direction == "bullish"
        else breakout_edge - target_info["multiple"] * range_height
    )
    # entry_zone (a volume-profile pullback pocket, POC-to-VAH/VAL) and
    # stop (a fixed fraction of the breakout range) are computed from two
    # independent reference frames — nothing otherwise guarantees the
    # zone stays clear of the stop. Confirmed live 2026-07-17: a real
    # report had entry_zone_low ($3989.23) sitting BELOW the stop
    # ($3989.70) on a BUY — invalid (stop must be below entry), and a
    # fill at the bottom of that zone would have been instantly stopped
    # out or rejected outright. Clamp with a minimum buffer so a fill
    # anywhere in the zone always keeps a meaningful risk distance.
    if direction == "bullish":
        entry_zone_low, entry_zone_high = poc, vah
        stop = breakout_edge - _ORB_SL_RANGE_PCT * range_height
        min_entry = stop + _ORB_MIN_ENTRY_STOP_BUFFER_PCT * range_height
        entry_zone_low = max(entry_zone_low, min_entry)
        entry_zone_high = max(entry_zone_high, entry_zone_low)
    else:
        entry_zone_low, entry_zone_high = val, poc
        stop = breakout_edge + _ORB_SL_RANGE_PCT * range_height
        max_entry = stop - _ORB_MIN_ENTRY_STOP_BUFFER_PCT * range_height
        entry_zone_high = min(entry_zone_high, max_entry)
        entry_zone_low = min(entry_zone_low, entry_zone_high)

    entry_mid = (entry_zone_low + entry_zone_high) / 2
    risk = abs(entry_mid - stop)
    reward = abs(target - entry_mid)
    rr = round(reward / risk, 2) if risk > 0 else None

    report.update({
        "entry_zone_low":  entry_zone_low,
        "entry_zone_high": entry_zone_high,
        "stop":            stop,
        "target":          target,
        "target_multiple": target_info["multiple"],
        "target_sample_n": target_info["n"],
        "target_is_default": target_info["is_default"],
        "sl_range_pct": _ORB_SL_RANGE_PCT,
        "entry_mid": entry_mid, "risk": risk, "reward": reward, "rr": rr,
        "position_note": (
            f"{direction} breakout of the pre-London range — "
            f"{abs(current_price - breakout_edge):.1f} pts past the "
            f"{'High' if direction == 'bullish' else 'Low'}"
        ),
    })
    return report


async def get_orb_target_multiple(bridge: Any) -> dict:
    """Cached wrapper — the backtest only needs to run once per day."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cached_date = await db_module.to_db_thread(db_module.get_app_config, "orb_target_multiple_date")
    if cached_date == today_str:
        cached = await db_module.to_db_thread(db_module.get_app_config, "orb_target_multiple")
        cached_n = await db_module.to_db_thread(db_module.get_app_config, "orb_target_multiple_n")
        if cached:
            try:
                n = int(cached_n or 0)
                return {"multiple": float(cached), "n": n, "is_default": n < _ORB_MIN_SAMPLES}
            except ValueError:
                pass
    result = await backtest_orb_target_multiple(bridge)
    await db_module.to_db_thread(db_module.set_app_config, "orb_target_multiple", str(result["multiple"]))
    await db_module.to_db_thread(db_module.set_app_config, "orb_target_multiple_n", str(result["n"]))
    await db_module.to_db_thread(db_module.set_app_config, "orb_target_multiple_date", today_str)
    return result


async def backtest_orb_target_multiple(bridge: Any) -> dict:
    """
    Genuine, disclosed analog to the video's proprietary "protection
    level" — measures, over this account's own recent gold history, how
    far price actually travelled past the pre-London range on days it
    cleanly broke one side only after London open, expressed as a
    multiple of that day's range height. Falls back to the standard
    ORB-literature 2x default if there isn't yet enough clean-breakout
    history.

    Reference range matches build_orb_report's own basis (see that
    function's docstring) — must stay in sync, since a mismatch here would
    calibrate the multiplier against a different-sized unit than what
    the live report actually multiplies it by.
    """
    from zoneinfo import ZoneInfo

    multiples: list[float] = []
    now_utc = datetime.now(timezone.utc)
    for days_back in range(1, _ORB_BACKTEST_DAYS + 1):
        day_london = now_utc.astimezone(ZoneInfo("Europe/London")) - timedelta(days=days_back)
        if day_london.weekday() >= 5:
            continue
        open_local = day_london.replace(hour=8, minute=0, second=0, microsecond=0)
        w_end = open_local.astimezone(timezone.utc).timestamp()  # London open that day
        w_start = w_end - _ORB_RANGE_HOURS * 3600  # last hour before London open
        horizon_end = w_end + _ORB_BACKTEST_HORIZON_HOURS * 3600

        candles = await bridge.get_candles_range(w_start, horizon_end, timeframe="M1")
        if not candles:
            continue
        range_c = [c for c in candles if w_start <= c["ts"] < w_end]
        after_c = [c for c in candles if c["ts"] >= w_end]
        if not range_c or not after_c:
            continue
        r_high = max(c["high"] for c in range_c)
        r_low  = min(c["low"] for c in range_c)
        r_height = r_high - r_low
        if r_height <= 0:
            continue

        broke_up = any(c["high"] > r_high for c in after_c)
        broke_down = any(c["low"] < r_low for c in after_c)
        if broke_up and not broke_down:
            multiples.append((max(c["high"] for c in after_c) - r_high) / r_height)
        elif broke_down and not broke_up:
            multiples.append((r_low - min(c["low"] for c in after_c)) / r_height)
        # both or neither -> ambiguous/no clean breakout day, skip

    if len(multiples) < _ORB_MIN_SAMPLES:
        return {"multiple": _ORB_DEFAULT_TARGET_MULTIPLE, "n": len(multiples), "is_default": True}
    multiples.sort()
    median = multiples[len(multiples) // 2]
    return {"multiple": round(median, 2), "n": len(multiples), "is_default": False}
