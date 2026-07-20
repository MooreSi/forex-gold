"""ORB/IVB (Asian-range breakout) report + auto-execute -- extracted
verbatim (no logic changes) from core/engine.py's SimulationEngine.
build_orb_report/_get_orb_target_multiple/_backtest_orb_target_multiple/
_orb_auto_execute, as part of the core/engine.py migration series. See
docs/todo/refactor/core-orb-report-migration/020-*.md.

Never places, closes, or modifies a live order -- _orb_auto_execute creates
a pending (DB-only) signal via core_signals.create_signal, inert until the
existing zone-fill watcher later opens it.

`_compute_volume_profile` is ported verbatim as a private, pure helper (no
`self` in the original either). `is_active_trader_node` is taken as an
explicit bool (the caller's already-computed answer from
SimulationEngine._is_active_trader_node, which belongs to a separate,
not-yet-migrated cluster) rather than recomputed here.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from forex_trader.core import database as db_module
from forex_trader.core import telegram_alerts
from forex_trader.core.core_signals import create_signal
from forex_trader.core.models import STRATEGY_ORB_FIXED

log = logging.getLogger(__name__)

_ORB_BUCKETS = 40           # volume-profile price buckets across the reference range
_ORB_VALUE_AREA_PCT = 0.70  # standard 70% value-area convention
_ORB_SL_RANGE_PCT = 0.50
_ORB_MIN_ENTRY_STOP_BUFFER_PCT = 0.30

_ORB_BACKTEST_DAYS = 25
_ORB_BACKTEST_HORIZON_HOURS = 10   # rest of London + all of NY session
_ORB_DEFAULT_TARGET_MULTIPLE = 2.0  # standard ORB convention, used until enough real history exists
_ORB_MIN_SAMPLES = 8


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
    Reference range is the Asian session (00:00-08:00 UTC, the same
    calendar day) rather than a freshly-forming first-hour-of-London
    range. Standard London-breakout convention trades the breakout of
    the ALREADY-ESTABLISHED Asian range the moment London opens — not a
    brand-new range built from London's own first hour, which this
    report used until 2026-07-17.

    Why: the Asian range averages ~4x wider than the old London-hour
    range (~48pt vs ~13pt over a 14-day sample, 2026-07-15). Building a
    volume-profile stop from an already-13pt window left nowhere for
    VAL/VAH to spread — confirmed live: all 4 real orb_fixed trades to
    date hit SL, 3 of them with a stop under 1pt on gold (smaller than
    typical spread). Switching the reference range to the wider Asian
    session, and deriving the stop from a fixed fraction of ITS height
    (_ORB_SL_RANGE_PCT) instead of an inner volume-profile boundary,
    fixes both: the range itself is more stable, and the stop no longer
    depends on how tightly volume happens to cluster within it.

    Also means the report is available the instant London opens rather
    than only after waiting out a full extra hour for a new range to
    form — the Asian range is already complete by then.
    """
    from zoneinfo import ZoneInfo

    tick = await bridge.get_tick()
    if tick is None:
        return None

    now_utc = datetime.now(timezone.utc)
    london_now = now_utc.astimezone(ZoneInfo("Europe/London"))
    london_open = london_now.replace(hour=8, minute=0, second=0, microsecond=0)
    window_start = london_open.astimezone(timezone.utc).timestamp()
    now_ts = now_utc.timestamp()

    if now_ts < window_start:
        return None  # London hasn't opened yet this cycle

    asia_start = london_open.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    asia_end = asia_start + 8 * 3600
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
                f"still inside the Asian range — {current_price - range_low:.1f} pts "
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
            f"{direction} breakout of the Asian range — "
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
    far price actually travelled past the Asian session range on days
    it cleanly broke one side only after London open, expressed as a
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
        w_start = open_local.astimezone(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()  # Asian session start (00:00 UTC same day)
        w_end = w_start + 8 * 3600  # Asian session end / London open
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


async def orb_auto_execute(report: dict, bridge: Any, is_active_trader_node: bool) -> None:
    """
    Places the ORB/IVB Report's recommended trade unattended, every
    morning, when the auto-execute toggle is on.

    This scheduler runs independently and unconditionally on *both* Mac
    and VPS (same as the email report above it). Normally it deliberately
    does NOT forward to the other node the way the Trading tab's Execute
    Trade button does — that button forwards because it's a single user
    click on one specific node's UI with nowhere else to go, whereas here
    both nodes compute the same report on the same clock, so only the
    active trader should ever act, or the same trade fires twice.

    Under centralized signal generation (Settings > Remote Node), that
    inverts: the VPS has stopped analyzing entirely
    (should_generate_signals_here() is False there), so it must defer
    even though it's the active trader — and the Mac proceeds even
    though it *isn't* the active trader, since open_trade() (called via
    open_manual_market_order() below) forwards the resolved trade to the
    VPS for execution under that mode. Still exactly one node ever acts:
    whichever one currently owns generation.
    """
    _active_here = is_active_trader_node
    try:
        from forex_trader.sync import server as _sync_srv_mod
        _is_vps = _sync_srv_mod.get_instance() is not None
    except ImportError:
        _is_vps = False
    _centralized = bool((await db_module.to_db_thread(db_module.get_risk_settings))
                        .get("centralized_signal_gen_enabled"))
    if _is_vps:
        _proceed = _active_here and not _centralized
    else:
        _proceed = _active_here or _centralized
    if not _proceed:
        return

    direction = report.get("direction")
    if direction not in ("bullish", "bearish"):
        log.info("[ORB auto-execute] no breakout yet — skipping")
        return

    mt5_direction = "BUY" if direction == "bullish" else "SELL"
    rs = await db_module.to_db_thread(db_module.get_risk_settings)
    _lot_val = float(rs.get("orb_lot_size", 0) or 0)
    lot_size = _lot_val if _lot_val > 0 else None

    # open_trade_from_signal() (called by the pending-fill watcher below)
    # resolves its strategy via the same per-channel override lookup as
    # every Telegram signal — auto-bootstrap it to STRATEGY_ORB_FIXED
    # once, the same lazy-configure-on-first-sight pattern already used
    # for Telegram channel parser configs, so this report's exact
    # stop/target survive unmanaged rather than being overridden by
    # whatever the global Active Strategy happens to be that morning.
    if db_module.get_channel_strategy_override("ORB/IVB Report (auto)") is None:
        await db_module.to_db_thread(
            db_module.set_channel_strategy_override,
            "ORB/IVB Report (auto)", STRATEGY_ORB_FIXED,
        )

    # Was open_manual_market_order() — a genuine MARKET order at whatever
    # price is live the instant this fires (right at window_end, e.g.
    # 08:00:20). That's the button semantics for the ORB tab's own manual
    # "Execute Trade" click (a deliberate in-the-moment action), but wrong
    # here: the whole point of the "Reload Zone Setup" this report emails
    # out is a *volume-profile entry zone* (POC-to-VAH/VAL) to wait for a
    # retracement into after the breakout — not "buy/sell immediately".
    # Confirmed live 2026-07-16: reported entry zone was $4035.29-4035.31,
    # actual auto-executed fill was $4030.24 — 5+pts away, because the
    # code never referenced entry_zone_low/entry_zone_high at all, just
    # fired at market. Now creates a pending signal at the reload zone
    # instead, using the same zone-fill watcher (_try_activate_pending_signals)
    # every other zone-entry signal in the app already goes through — it
    # only actually opens a trade once price genuinely re-enters the zone,
    # and expires unfilled (see the ORB-specific window there) if it never
    # does, rather than chasing price wherever it happens to be.
    try:
        sig = create_signal(
            source_name="ORB/IVB Report (auto)", direction=mt5_direction,
            entry_low=report["entry_zone_low"], entry_high=report["entry_zone_high"],
            stop_loss=report["stop"], tp1=report["target"],
            lot_size=lot_size,
        )
        log.info(
            "[ORB auto-execute] pending %s signal_id=%s zone=%.2f-%.2f stop=%.2f target=%.2f",
            mt5_direction, sig["signal_id"], report["entry_zone_low"], report["entry_zone_high"],
            report["stop"], report["target"],
        )
    except Exception as e:
        log.warning("[ORB auto-execute] execution failed: %s", e)
        asyncio.create_task(telegram_alerts.send_message(
            f"*ORB/IVB Auto-Execute Failed*\nDirection: {mt5_direction}\nError: {e}",
            None, "orb_auto_execute_failed",
        ))
