"""Real-time (3-second) level-cross detection for the Breakout Engine --
extracted verbatim (no logic changes) from engine.py's _velocity_loop/
_check_velocity_break as part of task 030. See
docs/todo/refactor/breakout-signal-migration/030-*.md.

_VelocityMixin is composed into BreakoutEngine (breakout_signal_service.py)
-- relies on self._cached (updated by the main cycle) and
self._price_history/_velocity_cooldowns (this mixin's own rolling state,
initialized in the service's __init__).
"""
from __future__ import annotations

import asyncio
import logging
import time

from backend.src.services.breakout_signal import breakout_signal_repo as bdb
from backend.src.services.breakout_signal import adaptive_params as ap
from backend.src.services.breakout_signal.signal_generator import (
    get_session,
    session_is_active,
    is_news_window,
    _is_compressed,
)

_log = logging.getLogger("breakout_signal")

_VELOCITY_INTERVAL = 3
_VELOCITY_WINDOW    = 20
_VELOCITY_COOLDOWN  = 90


class _VelocityMixin:
    async def _velocity_loop(self) -> None:
        while self.is_running:
            try:
                await self._check_velocity_break()
            except asyncio.CancelledError:
                break
            except Exception:
                pass
            await asyncio.sleep(_VELOCITY_INTERVAL)

    async def _check_velocity_break(self) -> None:
        """
        Sample the current price every 3 seconds. If price has crossed a key
        level by > min_break_pts within the last 20 seconds, fire a break-and-go
        signal immediately without waiting for the next M5 candle close.
        """
        cached = self._cached
        if not cached["key_levels"] or cached["adx"] <= 0:
            return

        session = get_session()
        if not session_is_active(session):
            return

        if session == "asian":
            return

        if is_news_window():
            return

        open_sigs = bdb.get_open_signals()
        if len(open_sigs) >= 2:
            return

        try:
            tick = await self._bridge.get_tick()
            if not tick:
                return
        except Exception:
            return

        mid   = (float(tick.bid) + float(tick.ask)) / 2
        now   = time.time()
        cutoff = now - _VELOCITY_WINDOW

        self._price_history.append((now, mid))
        self._price_history = [(t, p) for t, p in self._price_history if t >= cutoff]

        if len(self._price_history) < 4:
            return

        oldest_price = self._price_history[0][1]
        adx          = cached["adx"]
        htf_bias     = cached["htf_bias"]
        h4_bias      = cached["h4_bias"]
        atr          = cached["atr"]
        macd_hist    = cached["macd_hist"]
        key_levels   = cached["key_levels"]
        min_break    = ap.get("min_break_pts")
        min_adx_go   = ap.get("min_adx_go")
        dual_bias    = ap.get("require_dual_bias") >= 0.5

        if adx < min_adx_go or adx > ap.get("max_adx_entry"):
            return

        if atr < 7.0:
            return

        _m5_cached = cached.get("m5_candles") or []
        if not _is_compressed(_m5_cached, atr, ap.get("compression_max_range_atr")):
            return

        for level_info in key_levels:
            level    = float(level_info.get("price", 0) or 0)
            ltype    = level_info.get("type", "level")
            strength = int(level_info.get("strength", 1))
            if level <= 0:
                continue

            if (htf_bias in ("bullish", "neutral") and
                    (not dual_bias or h4_bias in ("bullish", "neutral")) and
                    macd_hist > 0 and
                    oldest_price < level and
                    mid > level + min_break):

                cooldown_key = f"BUY:{level:.0f}"
                last_fired = self._velocity_cooldowns.get(cooldown_key, 0)
                if now - last_fired < _VELOCITY_COOLDOWN:
                    continue

                if any(s.get("direction") == "BUY" for s in open_sigs):
                    continue

                _log.info(
                    "[BO-Velocity] BUY break detected: price %.2f crossed %.2f "
                    "(was %.2f, +%.1fpts in %.0fs | ADX %.1f)",
                    mid, level, oldest_price, mid - level,
                    now - self._price_history[0][0], adx,
                )
                self._velocity_cooldowns[cooldown_key] = now
                candidate = {
                    "direction":        "BUY",
                    "breakout_type":    "go",
                    "broken_level":     level,
                    "broken_level_type": ltype,
                    "level_strength":   strength,
                    "pts_beyond":       round(mid - level, 2),
                    "trigger":          "velocity",
                }
                context = {
                    "adx":       adx,
                    "macd_hist": macd_hist,
                    "htf_bias":  htf_bias,
                    "h4_bias":   h4_bias,
                    "session":   session,
                    "price":     float(tick.ask),
                    "atr_m15":   atr,
                    "trigger":   "velocity",
                }
                await self._process_candidate(
                    candidate, context, {"ts": now}, atr, adx, velocity=True, tick=tick
                )
                return

            if (htf_bias in ("bearish", "neutral") and
                    (not dual_bias or h4_bias in ("bearish", "neutral")) and
                    macd_hist < 0 and
                    oldest_price > level and
                    mid < level - min_break):

                cooldown_key = f"SELL:{level:.0f}"
                last_fired = self._velocity_cooldowns.get(cooldown_key, 0)
                if now - last_fired < _VELOCITY_COOLDOWN:
                    continue

                if any(s.get("direction") == "SELL" for s in open_sigs):
                    continue

                _log.info(
                    "[BO-Velocity] SELL break detected: price %.2f crossed %.2f "
                    "(was %.2f, -%.1fpts in %.0fs | ADX %.1f)",
                    mid, level, oldest_price, level - mid,
                    now - self._price_history[0][0], adx,
                )
                self._velocity_cooldowns[cooldown_key] = now
                candidate = {
                    "direction":        "SELL",
                    "breakout_type":    "go",
                    "broken_level":     level,
                    "broken_level_type": ltype,
                    "level_strength":   strength,
                    "pts_beyond":       round(level - mid, 2),
                    "trigger":          "velocity",
                }
                context = {
                    "adx":       adx,
                    "macd_hist": macd_hist,
                    "htf_bias":  htf_bias,
                    "h4_bias":   h4_bias,
                    "session":   session,
                    "price":     float(tick.bid),
                    "atr_m15":   atr,
                    "trigger":   "velocity",
                }
                await self._process_candidate(
                    candidate, context, {"ts": now}, atr, adx, velocity=True, tick=tick
                )
                return
