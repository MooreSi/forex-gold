"""ORB/IVB (pre-London-range breakout) report + auto-execute -- extracted
verbatim (no logic changes) from core/engine.py's SimulationEngine.
build_orb_report/_get_orb_target_multiple/_backtest_orb_target_multiple/
_orb_auto_execute, as part of the core/engine.py migration series. See
docs/todo/refactor/core-orb-report-migration/020-*.md.

orb_auto_execute() places a genuine EA pending limit order at the reload
zone (2026-07-22) -- was previously a DB-only pending signal
(core_signals.create_signal) later filled at market by the generic
zone-fill watcher; see that function's own docstring for why this changed.
No Python-bridge fallback exists for this path, same as Limit Runner
(core_limit_order_signal.py) -- if the EA isn't connected, the setup is
simply not captured that morning rather than falling back to the old
simulated-fill mechanism.

`_compute_volume_profile` is ported verbatim as a private, pure helper (no
`self` in the original either). `is_active_trader_node` is taken as an
explicit bool (the caller's already-computed answer from
SimulationEngine._is_active_trader_node, which belongs to a separate,
not-yet-migrated cluster) rather than recomputed here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from forex_trader.core import database as db_module
from forex_trader.core import telegram_alerts
from forex_trader.core.models import STRATEGY_ORB_FIXED

log = logging.getLogger(__name__)

_ORB_PENDING_EXPIRE_MINUTES = 60.0  # matches the pre-2026-07-22 zone-watcher's own ORB-specific expiry

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

    # Channel Strategy override (Trading > Strategy) -- same resolution
    # order as core_signal_resolution.resolve_open_trade_params: manual
    # override > auto-Claude rec > this channel's own default (orb_fixed,
    # not the global Active Strategy, since orb_fixed is what actually
    # suits a single-target breakout entry).
    from forex_trader.core import core_ea_templates as ea_templates
    _ch_override = await db_module.to_db_thread(
        db_module.get_channel_strategy_override, "ORB/IVB Report (auto)"
    )
    if _ch_override == "auto":
        _rec = await db_module.to_db_thread(
            db_module.get_channel_strategy_rec, "ORB/IVB Report (auto)"
        )
        strategy = _rec.get("strategy") or STRATEGY_ORB_FIXED
    elif _ch_override:
        strategy = _ch_override
    else:
        strategy = STRATEGY_ORB_FIXED

    if ea_templates.is_template_override(strategy):
        _tpl_name = ea_templates.template_name_from_override(strategy)
        log.info("[ORB auto-execute] channel assigned to EA Template '%s' -- not supported "
                 "for ORB/IVB's pending zone-entry order, skipping this morning", _tpl_name)
        asyncio.create_task(telegram_alerts.send_message(
            f"*ORB/IVB Auto-Execute Skipped*\nChannel Strategy for 'ORB/IVB Report' is set to "
            f"EA Template '{_tpl_name}', which isn't supported for ORB/IVB's pending zone-entry "
            f"order (EA Templates only manage immediate-fill trades). Reassign it to a regular "
            f"strategy in Trading > Strategy > Channel Strategy.",
            None, "orb_auto_execute_skipped",
        ))
        return

    from forex_trader.core import ea_bridge as _ea_mod
    _ea = _ea_mod.get_instance()
    if _ea is None or not _ea.is_ea_healthy():
        log.info("[ORB auto-execute] EA not connected/healthy — setup not captured")
        asyncio.create_task(telegram_alerts.send_message(
            f"*ORB/IVB Auto-Execute Skipped*\nDirection: {mt5_direction}\n"
            f"EA not connected — no Python-bridge fallback for this strategy.",
            None, "orb_auto_execute_skipped",
        ))
        return

    # Near edge of the reload zone (the price side reached first as the
    # market retraces back into it after the breakout) — same convention
    # place_pending_order()'s other callers use, see core_limit_order_signal.py.
    price = report["entry_zone_high"] if mt5_direction == "BUY" else report["entry_zone_low"]
    stop_loss = report["stop"]
    target = report["target"]

    _lot_val = float(rs.get("orb_lot_size", 0) or 0)
    if _lot_val > 0:
        lot = _lot_val
    else:
        from forex_trader.core.core_close_trade import get_trading_balance
        from forex_trader.core.core_fees_sizing import suggest_lot_size
        balance = await get_trading_balance(bridge, 1000.0)
        lot = suggest_lot_size(price, stop_loss, balance, float(rs.get("risk_per_trade_pct", 0.5)))

    trade_id = str(uuid.uuid4())[:16]
    try:
        ack = await _ea.place_pending_order(
            trade_id, mt5_direction, price, lot, stop_loss,
            {1: target}, [1.0], 0, strategy,
            expire_minutes=_ORB_PENDING_EXPIRE_MINUTES, close_full_on_last=True,
        )
    except Exception as e:
        log.warning("[ORB auto-execute] place_pending_order failed: %s", e)
        asyncio.create_task(telegram_alerts.send_message(
            f"*ORB/IVB Auto-Execute Failed*\nDirection: {mt5_direction}\nError: {e}",
            None, "orb_auto_execute_failed",
        ))
        return

    if ack.get("type") != "pending_order_placed":
        err = ack.get("error", "unknown error")
        log.warning("[ORB auto-execute] EA rejected pending order: %s", err)
        asyncio.create_task(telegram_alerts.send_message(
            f"*ORB/IVB Auto-Execute Rejected*\nDirection: {mt5_direction}\nError: {err}",
            None, "orb_auto_execute_rejected",
        ))
        return

    ticket = ack.get("ticket")
    now = time.time()
    signal_id = str(uuid.uuid4())[:16]
    with db_module.db() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                tp1,lot_size,notes,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, "ORB/IVB Report (auto)", mt5_direction,
             report["entry_zone_low"], report["entry_zone_high"], stop_loss, target, lot,
             f"ORB/IVB pending order @ {price:.2f} (EA ticket {ticket})",
             "pending", now),
        )
        conn.execute(
            """INSERT INTO vantage_pending_orders
               (trade_id,signal_id,tg_message_id,channel_name,direction,price,stop_loss,
                tps_json,pcts_json,be_at_pos,tp_open,lot_size,ea_ticket,status,created_at,strategy)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, signal_id, None, "ORB/IVB Report (auto)", mt5_direction, price, stop_loss,
             json.dumps({1: target}), json.dumps([1.0]), 0, 0, lot, ticket,
             "working", now, strategy),
        )
    log.info(
        "[ORB auto-execute] pending %s ticket=%s @ %.2f stop=%.2f target=%.2f lot=%.2f",
        mt5_direction, ticket, price, stop_loss, target, lot,
    )
    asyncio.create_task(telegram_alerts.send_message(
        f"*ORB/IVB Limit Order Placed*\nDirection: {mt5_direction}\n"
        f"Price: {price:.2f}\nStop: {stop_loss:.2f}\nTarget: {target:.2f}\n"
        f"Lot: {lot:g}\nEA ticket: {ticket}",
        None, "orb_auto_execute_placed",
    ))
