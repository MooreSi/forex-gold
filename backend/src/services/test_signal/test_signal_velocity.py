"""Real-time (3-second) velocity spike + liquidity sweep monitor for
TestSignalEngine (Bounce) -- extracted verbatim (no logic changes) from
engine.py's _velocity_loop as part of task 030. See
docs/todo/refactor/test-signal-migration/030-*.md.

_VelocityMixin is composed into TestSignalEngine (test_signal_service.py)
-- relies on self._cached_swing_high/_low, self._sweep_touch_*,
self._cached_candles, self._force_scan/_force_scan_event, and
self._last_emergency_scan/_last_candle_refresh, all initialized in the
service's __init__.
"""
from __future__ import annotations

import asyncio
import logging
import time

_log = logging.getLogger("test_signal")

_VELOCITY_INTERVAL      = 3
_VELOCITY_WINDOW        = 20
_VELOCITY_THRESHOLD     = 10.0
_SWEEP_REVERSAL_PT      = 3.0
_SWEEP_TIMEOUT          = 30
_EMERGENCY_SCAN_COOLDOWN = 120
_CANDLE_REFRESH_INTERVAL = 30.0


def _compute_swing_levels(m15_candles: list) -> tuple[float, float]:
    """Return (swing_high, swing_low) from the last 20 M15 bars."""
    if len(m15_candles) < 5:
        return 0.0, 0.0
    recent = m15_candles[-20:]
    highs = [float(c.get("high", 0) or 0) for c in recent]
    lows  = [float(c.get("low",  0) or 0) for c in recent]
    return max(highs), min(l for l in lows if l > 0)


class _VelocityMixin:
    async def _velocity_loop(self) -> None:
        """
        Samples price every _VELOCITY_INTERVAL seconds.  Fires an emergency scan
        (bypassing the candle gate) when either condition is met:

        1. Velocity spike: price moves >= _VELOCITY_THRESHOLD pts within
           _VELOCITY_WINDOW seconds — rapid directional move signals a potential
           reversal setup the candle gate would have missed entirely.

        2. Liquidity sweep: price touches/crosses a cached M15 swing high or low
           then reverses by >= _SWEEP_REVERSAL_PT pts within _SWEEP_TIMEOUT seconds
           — institutional stop-hunt followed by sharp rejection.
        """
        import collections
        _max_samples = int(_VELOCITY_WINDOW / _VELOCITY_INTERVAL) + 2
        _prices: collections.deque = collections.deque(maxlen=_max_samples)
        _prev_mid: float = 0.0
        _last_remote_check = 0.0
        _is_remote = False

        while self._running:
            try:
                _now_chk = time.time()
                if _now_chk - _last_remote_check >= _CANDLE_REFRESH_INTERVAL:
                    from forex_trader.core import database as _db_module
                    _is_remote = await _db_module.to_db_thread(_db_module.is_remote_node)
                    _last_remote_check = _now_chk
                if _is_remote:
                    await asyncio.sleep(_VELOCITY_INTERVAL)
                    continue

                tick = await self._bridge.get_tick()
                if tick:
                    mid = (float(tick.bid) + float(tick.ask)) / 2
                    now = time.time()
                    _prices.append((now, mid))

                    # ── 1. Velocity check ─────────────────────────────────────
                    window_start = now - _VELOCITY_WINDOW
                    window_prices = [(t, p) for t, p in _prices if t >= window_start]
                    if len(window_prices) >= 2:
                        oldest_px  = window_prices[0][1]
                        elapsed    = now - window_prices[0][0]
                        velocity   = abs(mid - oldest_px)
                        cooldown_ok = (now - self._last_emergency_scan) > _EMERGENCY_SCAN_COOLDOWN
                        if velocity >= _VELOCITY_THRESHOLD and cooldown_ok and not self._force_scan:
                            direction_lbl = "UP" if mid > oldest_px else "DOWN"
                            _log.info(
                                "[FastMonitor] Velocity spike %.2f pts in %.0fs (%s) — emergency scan queued",
                                velocity, elapsed, direction_lbl,
                            )
                            self._force_scan = True
                            if self._force_scan_event is not None:
                                self._force_scan_event.set()

                    # ── 2. Liquidity sweep check ──────────────────────────────
                    sh  = self._cached_swing_high
                    slo = self._cached_swing_low
                    cooldown_ok = (now - self._last_emergency_scan) > _EMERGENCY_SCAN_COOLDOWN

                    if sh > 0 and _prev_mid > 0:
                        # Swept swing HIGH — price touched/crossed it from below
                        if _prev_mid < sh and mid >= sh:
                            self._sweep_touch_high = mid
                            self._sweep_touch_time = now
                            _log.debug("[FastMonitor] Swing HIGH %.2f touched @ %.2f", sh, mid)

                    if self._sweep_touch_high > 0:
                        if (now - self._sweep_touch_time) > _SWEEP_TIMEOUT:
                            self._sweep_touch_high = 0.0
                        elif mid <= self._sweep_touch_high - _SWEEP_REVERSAL_PT:
                            if cooldown_ok and not self._force_scan:
                                _log.info(
                                    "[FastMonitor] Liquidity sweep HIGH %.2f → reversal to %.2f"
                                    " (%.2f pts) — emergency scan queued",
                                    self._sweep_touch_high, mid,
                                    self._sweep_touch_high - mid,
                                )
                                self._force_scan = True
                                if self._force_scan_event is not None:
                                    self._force_scan_event.set()
                            self._sweep_touch_high = 0.0

                    if slo > 0 and _prev_mid > 0:
                        # Swept swing LOW — price touched/crossed it from above
                        if _prev_mid > slo and mid <= slo:
                            self._sweep_touch_low = mid
                            self._sweep_touch_time = now
                            _log.debug("[FastMonitor] Swing LOW %.2f touched @ %.2f", slo, mid)

                    if self._sweep_touch_low > 0:
                        if (now - self._sweep_touch_time) > _SWEEP_TIMEOUT:
                            self._sweep_touch_low = 0.0
                        elif mid >= self._sweep_touch_low + _SWEEP_REVERSAL_PT:
                            if cooldown_ok and not self._force_scan:
                                _log.info(
                                    "[FastMonitor] Liquidity sweep LOW %.2f → reversal to %.2f"
                                    " (%.2f pts) — emergency scan queued",
                                    self._sweep_touch_low, mid,
                                    mid - self._sweep_touch_low,
                                )
                                self._force_scan = True
                                if self._force_scan_event is not None:
                                    self._force_scan_event.set()
                            self._sweep_touch_low = 0.0

                    _prev_mid = mid

                # ── Background candle cache refresh ───────────────────────────
                # Keeps fresh candle data ready so emergency scans skip the
                # bridge round-trip entirely and fire Claude sooner.
                if (now - self._last_candle_refresh) >= _CANDLE_REFRESH_INTERVAL:
                    try:
                        h1  = await self._bridge.get_candles("H1",  120)
                        m15 = await self._bridge.get_candles("M15",  60)
                        h4  = await self._bridge.get_candles("H4",   40)
                        m5  = await self._bridge.get_candles("M5",   30)
                        if h1 and m15:
                            _ts = time.time()
                            self._cached_candles = {
                                "H1":  (_ts, h1),
                                "M15": (_ts, m15),
                                "H4":  (_ts, h4 or []),
                                "M5":  (_ts, m5 or []),
                            }
                            # Keep swing levels fresh from the cache refresh too
                            _sh, _sl = _compute_swing_levels(m15)
                            if _sh > 0:
                                self._cached_swing_high = _sh
                            if _sl > 0:
                                self._cached_swing_low = _sl
                            self._last_candle_refresh = _ts
                    except Exception as _ce:
                        _log.debug("[FastMonitor] Candle cache refresh error: %s", _ce)

            except asyncio.CancelledError:
                break
            except Exception as e:
                _log.debug("[FastMonitor] velocity loop error: %s", e)

            await asyncio.sleep(_VELOCITY_INTERVAL)
