"""
NativeMT5Bridge — in-process MT5 bridge for native Windows.

On the Mac, MT5 runs under Wine, and the MetaTrader5 Python package only
works inside Wine's own Python — mt5_bridge.py has to run as a separate
subprocess there, talking to the main app over local HTTP (MT5BridgeClient
in core/mt5_bridge.py). On native Windows that constraint doesn't exist:
the main app's own Python interpreter can import MetaTrader5 directly, since
it's the same interpreter mt5_bridge.py already runs under when launched as
a subprocess there. This class imports mt5_bridge.py as a plain module (its
functions are pure — the __main__ guard keeps the HTTP server from starting)
and calls them directly via asyncio.to_thread(), skipping the HTTP hop
entirely: no subprocess, no localhost socket, no JSON serialization.

Implements the exact same public async interface as MT5BridgeClient so
engine.py can use either interchangeably — see
wire_platform_bridge()/should_use_native() below for the selection point.

All MT5 native calls are serialized through a single asyncio.Lock, matching
the discipline already used by mt5_bridge.py's own _mt5_call_lock — the
MetaTrader5 package isn't documented as safe for concurrent calls from
multiple threads, and asyncio.to_thread() dispatches to a thread pool that
could otherwise run more than one at once.
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
import sys
import time
from pathlib import Path
from typing import Optional

from backend.src.utils.models import Tick, SYMBOL, POINT_SIZE, DIGITS

log = logging.getLogger(__name__)

TICK_CACHE_TTL = 1.0
CANDLE_CACHE_TTL = 5.0


def is_available() -> bool:
    """Native in-process MT5 is only meaningful on Windows — the
    MetaTrader5 package requires the native Windows MT5 terminal, and
    ctypes.windll (used for AutoTrading toggling) is Windows-only."""
    return sys.platform == "win32"


class NativeMT5Bridge:
    def __init__(self):
        self._mod = None
        self._lock = asyncio.Lock()
        self._tick_cache: Optional[Tick] = None
        self._tick_cache_ts: float = 0.0
        self._candle_cache: dict = {}
        self._candle_cache_ts: dict = {}

    @property
    def url(self) -> str:
        return "native (in-process)"

    def is_configured(self) -> bool:
        return self._mod is not None

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        from backend.src.utils.os_utils import repo_root
        bridge_path = repo_root() / "mt5_bridge.py"
        if not bridge_path.exists():
            log.error("NativeMT5Bridge: mt5_bridge.py not found at %s", bridge_path)
            return

        # mt5_bridge.py reads CONFIG_PATH from the BRIDGE_CREDS_PATH env var
        # at module-exec time (falls back to a sibling file next to the
        # script if unset). The subprocess path (_start_bridge_process) sets
        # this explicitly before launching; exec'ing the module in-process
        # skips that entirely unless set here too — this was the actual
        # cause of the "No credentials configured" errors on the VPS after
        # switching to the native bridge, since it was silently reading the
        # wrong (empty) credentials file location instead of the one the
        # app actually writes to via db_module.sync_bridge_credentials_file().
        import os as _os
        from backend.src.config import USER_DATA_DIR
        _os.environ.setdefault("BRIDGE_CREDS_PATH", str(USER_DATA_DIR / "bridge_credentials.json"))

        spec = importlib.util.spec_from_file_location("mt5_bridge_native_mod", bridge_path)
        mod = importlib.util.module_from_spec(spec)
        # exec_module runs the whole file top-level (imports MetaTrader5,
        # defines every function/global) but never calls main() — that's
        # gated behind `if __name__ == "__main__":`, and this module's
        # __name__ is "mt5_bridge_native_mod", not "__main__".
        await asyncio.to_thread(spec.loader.exec_module, mod)
        self._mod = mod
        log.info("NativeMT5Bridge: credentials path = %s", mod.CONFIG_PATH)
        connected = await asyncio.to_thread(mod._connect)
        if connected:
            log.info("NativeMT5Bridge: connected to MT5 in-process (no HTTP bridge)")
        else:
            log.warning("NativeMT5Bridge: initial connect failed (%s) — "
                        "background reconnect will keep retrying", mod._last_error)
        # Reuse mt5_bridge.py's own reconnect loop for resilience — it already
        # retries every RECONNECT_EVERY seconds when disconnected.
        import threading
        threading.Thread(target=mod._reconnect_loop, daemon=True).start()

    async def shutdown(self) -> None:
        if self._mod is not None:
            try:
                await asyncio.to_thread(self._mod._disconnect)
            except Exception:
                pass

    async def _call(self, fn_name: str, *args, timeout: float = 20.0, **kwargs):
        # Bounded wait — mt5_bridge.py's _connect()/_disconnect() now take the
        # same _mt5_call_lock every other call does (see the fix there), which
        # closes the race that let a reconnect tear down the IPC channel out
        # from under an in-flight call. But any call into an external terminal
        # process over IPC can still stall for reasons outside our control, and
        # this method has no timeout at all today — a single wedged call holds
        # self._lock forever, freezing every subsequent bridge-dependent
        # operation (which is nearly everything _monitor_loop does) for good.
        # Timing out here means the caller sees an error and the rest of the
        # app keeps running instead of the whole event loop freezing; the
        # existing bridge watchdog is what detects and recovers from a bridge
        # that's actually wedged.
        async with self._lock:
            fn = getattr(self._mod, fn_name)
            return await asyncio.wait_for(asyncio.to_thread(fn, *args, **kwargs), timeout=timeout)

    # ── Tick ─────────────────────────────────────────────────────────────────

    async def get_tick(self) -> Optional[Tick]:
        now = time.time()
        if self._tick_cache and (now - self._tick_cache_ts) < TICK_CACHE_TTL:
            return self._tick_cache
        tick = await self._fetch_tick()
        if tick:
            self._tick_cache = tick
            self._tick_cache_ts = now
        return tick

    async def get_fresh_tick(self) -> Optional[Tick]:
        tick = await self._fetch_tick()
        if tick:
            self._tick_cache = tick
            self._tick_cache_ts = time.time()
        return tick

    async def _fetch_tick(self) -> Optional[Tick]:
        if self._mod is None:
            return None
        try:
            d = await self._call("_get_tick")
            if not d:
                return None
            bid = float(d["bid"])
            ask = float(d["ask"])
            spread = round(ask - bid, 5)
            return Tick(
                bid=round(bid, DIGITS),
                ask=round(ask, DIGITS),
                mid=round((bid + ask) / 2, DIGITS),
                spread=spread,
                spread_points=round(spread / POINT_SIZE, 1),
                timestamp=float(d.get("timestamp", time.time())),
                source="mt5_vantage",
            )
        except Exception as e:
            log.debug("NativeMT5Bridge tick fetch failed: %s", e)
            return None

    # ── Candles ───────────────────────────────────────────────────────────────

    async def get_candles(self, timeframe: str = "M5", count: int = 200) -> list[dict]:
        now = time.time()
        key = f"{timeframe}_{count}"
        if key in self._candle_cache and (now - self._candle_cache_ts.get(key, 0)) < CANDLE_CACHE_TTL:
            return self._candle_cache[key]
        try:
            candles = await self._call("_get_candles", timeframe, count) or []
        except Exception as e:
            log.debug("NativeMT5Bridge candles fetch failed (%s): %s", timeframe, e)
            candles = []
        if candles:
            self._candle_cache[key] = candles
            self._candle_cache_ts[key] = now
        return candles

    async def get_candles_for_symbol(self, symbol: str,
                                     timeframe: str = "M5", count: int = 20) -> list[dict]:
        try:
            return await self._call("_get_candles_for_symbol", symbol, timeframe, count) or []
        except Exception as e:
            log.debug("NativeMT5Bridge candles_for_symbol(%s) fetch failed: %s", symbol, e)
            return []

    async def get_candles_range(self, from_ts: float, to_ts: float,
                                timeframe: str = "M1") -> list[dict]:
        try:
            return await self._call("_get_candles_range", from_ts, to_ts, timeframe, SYMBOL) or []
        except Exception as e:
            log.debug("NativeMT5Bridge candles_range fetch failed: %s", e)
            return []

    # ── Account / positions ───────────────────────────────────────────────────

    async def get_health(self) -> dict:
        if self._mod is None:
            return {"connected": False, "error": "native bridge not started"}
        try:
            # Bounded wait mirrors the HTTP bridge's /health handling — this
            # drives the app's bridge watchdog, so it must never block
            # indefinitely behind a slow call holding the lock.
            acquired = False
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout=1.0)
                acquired = True
            except asyncio.TimeoutError:
                pass
            try:
                mod = self._mod
                connected = mod._connected
                error = mod._last_error
                ct = mod._connect_time
                trade_allowed = None
                if acquired and connected and mod._MT5_AVAILABLE:
                    try:
                        term = await asyncio.to_thread(mod.mt5.terminal_info)
                        trade_allowed = bool(term.trade_allowed) if term else None
                    except Exception:
                        pass
                return {
                    "status":        "connected" if connected else "disconnected",
                    "connected":     connected,
                    "mt5_available": mod._MT5_AVAILABLE,
                    "symbol":        SYMBOL,
                    "last_error":    error,
                    "connect_time":  ct,
                    "trade_allowed": trade_allowed,
                }
            finally:
                if acquired:
                    self._lock.release()
        except Exception as e:
            return {"connected": False, "error": str(e)}

    async def get_account(self) -> Optional[dict]:
        try:
            return await self._call("_get_account")
        except Exception:
            return None

    async def get_positions(self) -> list[dict]:
        try:
            return await self._call("_get_positions") or []
        except Exception:
            return []

    # ── Orders ────────────────────────────────────────────────────────────────

    async def place_order(self, direction: str, lots: float,
                          sl: Optional[float], tp: Optional[float],
                          comment: str = "") -> dict:
        if self._mod is None:
            return {"error": "Native MT5 bridge not started"}
        try:
            return await self._call(
                "_place_order", direction, lots, sl, tp, comment or "ForexTrader",
            )
        except Exception as e:
            return {"error": str(e)}

    async def close_position(self, ticket: int) -> dict:
        if self._mod is None:
            return {"error": "Native MT5 bridge not started"}
        try:
            return await self._call("_close_position", ticket)
        except Exception as e:
            return {"error": str(e)}

    async def partial_close(self, ticket: int, lots: float) -> dict:
        if self._mod is None:
            return {"error": "Native MT5 bridge not started"}
        try:
            return await self._call("_partial_close", ticket, lots)
        except Exception as e:
            return {"error": str(e)}

    async def modify_order(self, ticket: int, sl: Optional[float], tp: Optional[float]) -> dict:
        if self._mod is None:
            return {"error": "Native MT5 bridge not started"}
        try:
            return await self._call("_modify_position", ticket, sl, tp)
        except Exception as e:
            return {"error": str(e)}

    async def send_credentials(self, login: int, password: str, server: str) -> dict:
        if self._mod is None:
            return {"error": "Native MT5 bridge not started"}
        try:
            return await self._call("_apply_credentials", login, password, server, "")
        except Exception as e:
            return {"error": str(e)}

    async def reconnect(self) -> dict:
        if self._mod is None:
            return {"error": "Native MT5 bridge not started"}
        try:
            async with self._lock:
                await asyncio.to_thread(self._mod._disconnect)
                await asyncio.sleep(1)
                ok = await asyncio.to_thread(self._mod._connect)
            return {"status": "connected" if ok else "failed", "error": self._mod._last_error}
        except Exception as e:
            return {"error": str(e)}

    async def enable_autotrading(self) -> dict:
        if self._mod is None:
            return {"enabled": False, "error": "native bridge not started"}
        try:
            return await self._call("_try_enable_autotrading")
        except Exception as e:
            return {"enabled": False, "error": str(e)}

    async def get_deal_history(self, days: int = 7) -> list[dict]:
        try:
            return await self._call("_get_history", days) or []
        except Exception:
            return []

    async def get_position_history(self, ticket: int) -> list[dict]:
        try:
            return await self._call("_get_history_by_position", ticket) or []
        except Exception:
            return []

    async def get_tick_at(self, ts: float) -> Optional[dict]:
        try:
            return await self._call("_get_tick_at", ts)
        except Exception:
            return None
