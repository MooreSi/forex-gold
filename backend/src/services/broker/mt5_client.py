"""
MT5 Bridge client.
Talks to mt5_bridge.py (the Wine Python process) over HTTP on localhost:9000.
All methods are async and return None / empty on failure rather than raising.

Uses a persistent httpx.AsyncClient (created in startup / destroyed in shutdown)
so the underlying TCP connection is kept alive across requests — avoids a
TCP handshake + client object setup overhead on every call.
"""

import logging
import time
from typing import Optional

import httpx

from backend.src.utils.models import Tick, SYMBOL, POINT_SIZE, DIGITS

log = logging.getLogger(__name__)


# ── "Could not look" is not "nothing there" (stage3/010) ──────────────────────
#
# `dedup.find_trade`'s own docstring states the contract: "None means 'could
# not look'; [] means 'nothing there'. Treating them alike is the mistake this
# whole module exists to prevent." It branches on both -- and until 2026-08-31
# neither real client could ever produce the None. Every failure came back as
# an empty list, so a failed position query read as "the broker has no record
# of this trade" and the order was sent again.
#
# The dedup unit tests passed throughout because their fake bridge raises or
# returns None. Production never produces either shape.
#
# Callers that genuinely do not care use `or []` at the call site, which is
# byte-for-byte today's behaviour. The ones that must know the difference --
# dedup and reconciliation -- already branch on None and now get told.



# ── Transport failures on a SEND (stage3/020) ─────────────────────────────────
#
# "The broker said no" and "nobody knows" are different answers, and the
# difference decides whether a signal is retried. mt5_bridge already flags the
# lost-answer case INSIDE the bridge process (order_send returning None). This
# is the other half: an answer lost between this app and the bridge.
#
# httpx tells the two apart. Nothing left this machine on a connect failure or
# a pool timeout, so nothing was placed and retrying is safe -- marking those
# unknown would park a signal every time the bridge is restarted, and only
# reconciliation could release it. Everything else at transport level happened
# with the request already on the wire, which means the bridge may have called
# order_send and only the reply was lost.
#
# Anything that is not recognisably a never-sent failure is treated as unknown.
# That is the conservative direction: a wrongly-parked signal waits for
# reconciliation, a wrongly-retried one can become two live orders.
_NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)


def _send_failure(exc: Exception) -> dict:
    """Shape a transport failure on a send path, marking it unknown unless the
    request provably never left."""
    out = {"error": str(exc) or type(exc).__name__}
    if not isinstance(exc, _NEVER_SENT):
        out["unknown"] = True
    return out


_tick_cache:    Optional[Tick] = None
_tick_cache_ts: float = 0.0
_candle_cache:  dict = {}
_candle_cache_ts: dict = {}

TICK_CACHE_TTL   = 1.0   # reduced from 2s — fresh enough for 1s fast-poll cycle
CANDLE_CACHE_TTL = 5.0   # M5 candles only close every 5 min; 5s is more than fresh enough

# Individual tick-fetch failures were logged at DEBUG — invisible with the
# app's default INFO log level, so self_healer.py's log-scanning "bridge_
# offline" pattern could never see them and never trigger its recovery
# action. A single failed request is normal noise (a slow request ahead of
# it, a one-off network blip) and shouldn't be logged loudly; SUSTAINED
# failure is the actual signal worth surfacing. Track consecutive failures
# and promote to WARNING (with wording the self-healer's regex matches)
# only once it looks like a real outage rather than a blip.
_tick_fail_streak = 0
_TICK_FAIL_WARN_THRESHOLD = 5

# A wedged connection pool (leaked/stuck keep-alive sockets) doesn't
# self-recover and is invisible from outside this client -- confirmed live
# 2026-07-23: every tick request through this client failed continuously
# for 30+ minutes, including across an app restart, while a direct curl to
# the bridge answered instantly the whole time. Restarting the Wine-side
# bridge process (self_healer.py's own remedy for "bridge not responding")
# does nothing for this failure mode since the server was never actually
# down; only a fresh httpx.AsyncClient clears it. Set well above the WARN
# threshold so a short blip never triggers a rebuild -- only a sustained
# streak does. Retried on every multiple (not just once) so a genuine
# server outage still gets periodic recovery attempts instead of giving up
# after the first one.
_CLIENT_RECYCLE_THRESHOLD = 30


class MT5BridgeClient:
    def __init__(self, bridge_url: str):
        self._url  = bridge_url.rstrip("/") if bridge_url else ""
        self._http: Optional[httpx.AsyncClient] = None
        self._recycling = False

    @property
    def url(self) -> str:
        return self._url

    def is_configured(self) -> bool:
        return bool(self._url)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        """Create a persistent HTTP client with keep-alive.  Call once on app start."""
        if not self._url:
            return
        self._http = httpx.AsyncClient(
            limits=httpx.Limits(
                # Raised from 2/5 -- too small for multiple concurrent browser
                # tabs, each running its own full set of per-client polling
                # loops against this one shared client. Under-provisioning
                # this pool was the proximate trigger for the 2026-07-23
                # incident: legitimate concurrent load exhausted it, and nothing
                # ever released the wedged connections afterward.
                max_keepalive_connections=10,
                max_connections=20,
                keepalive_expiry=30,
            ),
            timeout=httpx.Timeout(10.0),
        )
        log.info("MT5BridgeClient: persistent HTTP session created for %s", self._url)

    async def shutdown(self) -> None:
        """Close the persistent client.  Call on app shutdown."""
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception:
                pass
            self._http = None
        log.info("MT5BridgeClient: HTTP session closed")

    async def _recycle_http_client(self) -> None:
        """Tear down and rebuild the persistent client to clear a wedged
        connection pool. Guarded against concurrent callers (many tick
        fetches can cross the recycle threshold in the same instant)."""
        if self._recycling:
            return
        self._recycling = True
        try:
            log.warning(
                "MT5BridgeClient: %d consecutive tick failures — recycling HTTP client",
                _tick_fail_streak,
            )
            await self.shutdown()
            await self.startup()
        finally:
            self._recycling = False

    # ── Internal request helper ───────────────────────────────────────────────

    async def _request(self, method: str, url: str,
                       timeout: float = 10.0, **kwargs) -> httpx.Response:
        """Use the persistent client when available; fall back to a temporary one."""
        if self._http is not None:
            return await getattr(self._http, method)(url, timeout=timeout, **kwargs)
        async with httpx.AsyncClient(timeout=timeout) as tmp:
            return await getattr(tmp, method)(url, **kwargs)

    # ── Tick ─────────────────────────────────────────────────────────────────

    async def get_tick(self) -> Optional[Tick]:
        global _tick_cache, _tick_cache_ts
        now = time.time()
        if _tick_cache and (now - _tick_cache_ts) < TICK_CACHE_TTL:
            return _tick_cache
        tick = await self._fetch_tick()
        if tick:
            _tick_cache = tick
            _tick_cache_ts = now
        return tick

    async def get_fresh_tick(self) -> Optional[Tick]:
        """Always fetch from the bridge — bypass cache.  Use just before placing orders."""
        global _tick_cache, _tick_cache_ts
        tick = await self._fetch_tick()
        if tick:
            _tick_cache    = tick
            _tick_cache_ts = time.time()
        return tick

    async def _fetch_tick(self) -> Optional[Tick]:
        global _tick_fail_streak
        if not self._url:
            return None
        try:
            r = await self._request("get", f"{self._url}/tick/{SYMBOL}", timeout=4.0)
            r.raise_for_status()
            d = r.json()
            bid = float(d["bid"])
            ask = float(d["ask"])
            spread = round(ask - bid, 5)
            if _tick_fail_streak >= _TICK_FAIL_WARN_THRESHOLD:
                log.warning("bridge tick fetch recovered after %d consecutive failures",
                            _tick_fail_streak)
            _tick_fail_streak = 0
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
            _tick_fail_streak += 1
            if _tick_fail_streak >= _TICK_FAIL_WARN_THRESHOLD:
                # Wording deliberately matches self_healer.py's bridge_offline
                # pattern ("bridge.*not.*respond") so sustained failure is
                # both visible in the log and actionable by the auto-healer.
                log.warning("bridge not responding to tick requests (%d consecutive "
                            "failures): %s", _tick_fail_streak, e)
            else:
                log.debug("bridge tick fetch failed (%d/%d): %s",
                          _tick_fail_streak, _TICK_FAIL_WARN_THRESHOLD, e)
            if _tick_fail_streak % _CLIENT_RECYCLE_THRESHOLD == 0:
                await self._recycle_http_client()
            return None

    # ── Candles ───────────────────────────────────────────────────────────────

    async def get_candles(self, timeframe: str = "M5", count: int = 200) -> list[dict]:
        global _candle_cache, _candle_cache_ts
        now = time.time()
        key = f"{timeframe}_{count}"
        if key in _candle_cache and (now - _candle_cache_ts.get(key, 0)) < CANDLE_CACHE_TTL:
            return _candle_cache[key]
        candles = await self._fetch_candles(timeframe, count)
        if candles:
            _candle_cache[key] = candles
            _candle_cache_ts[key] = now
        return candles

    async def _fetch_candles(self, timeframe: str, count: int) -> list[dict]:
        if not self._url:
            return []
        try:
            r = await self._request("get", f"{self._url}/candles/{SYMBOL}",
                                    timeout=10.0,
                                    params={"timeframe": timeframe, "count": count})
            r.raise_for_status()
            return r.json().get("candles", [])
        except Exception as e:
            log.debug("bridge candles fetch failed (%s): %s", timeframe, e)
            return []

    async def get_candles_for_symbol(self, symbol: str,
                                     timeframe: str = "M5", count: int = 20) -> list[dict]:
        if not self._url:
            return []
        # Large bulk fetches (backtest) can take up to 60 s for MT5 to pull
        # thousands of bars from its local cache; use a generous timeout.
        fetch_timeout = max(60.0, count / 200.0)
        try:
            r = await self._request("get", f"{self._url}/candles_symbol/{symbol}",
                                    timeout=fetch_timeout,
                                    params={"timeframe": timeframe, "count": count})
            r.raise_for_status()
            return r.json().get("candles", [])
        except Exception as e:
            log.debug("bridge candles_symbol(%s) fetch failed: %s", symbol, e)
            return []

    async def get_candles_range(self, from_ts: float, to_ts: float,
                                timeframe: str = "M1") -> list[dict]:
        """Fetch XAUUSD M1 candles between two Unix timestamps (for post-trade analysis)."""
        if not self._url:
            return []
        try:
            r = await self._request(
                "get", f"{self._url}/candles_range",
                timeout=30.0,
                params={"from": str(from_ts), "to": str(to_ts), "timeframe": timeframe},
            )
            r.raise_for_status()
            return r.json().get("candles", [])
        except Exception as e:
            log.debug("bridge candles_range fetch failed: %s", e)
            return []

    async def get_ticks_range(self, from_ts: float, to_ts: float) -> list[dict]:
        """Fetch XAUUSD ticks between two Unix timestamps -- bounded to one
        day by the bridge itself (docs/todo/backtest/010 phase 1; a measured
        hour is 1.7-2.0MB, so a full range request is tens of MB)."""
        if not self._url:
            return []
        try:
            r = await self._request(
                "get", f"{self._url}/ticks",
                timeout=60.0,
                params={"from": str(from_ts), "to": str(to_ts)},
            )
            r.raise_for_status()
            return r.json().get("ticks", [])
        except Exception as e:
            log.debug("bridge ticks fetch failed: %s", e)
            return []

    # ── Account / positions ───────────────────────────────────────────────────

    async def get_health(self) -> dict:
        if not self._url:
            return {"connected": False, "error": "bridge url not configured"}
        try:
            r = await self._request("get", f"{self._url}/health", timeout=4.0)
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            return {"connected": False, "error": str(e)}
        return {"connected": False}

    async def get_account(self) -> Optional[dict]:
        if not self._url:
            return None
        try:
            r = await self._request("get", f"{self._url}/account", timeout=4.0)
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None

    async def get_positions(self) -> Optional[list[dict]]:
        if not self._url:
            return None      # not configured: we cannot look
        try:
            r = await self._request("get", f"{self._url}/positions", timeout=4.0)
            if r.status_code == 200:
                return r.json().get("positions", [])
        except Exception:
            return None
        return None

    # ── Orders ────────────────────────────────────────────────────────────────

    async def place_order(self, direction: str, lots: float,
                          sl: Optional[float], tp: Optional[float],
                          comment: str = "") -> dict:
        if not self._url:
            return {"error": "MT5 bridge not configured"}
        try:
            r = await self._request("post", f"{self._url}/order", timeout=15.0,
                                    json={
                                        "direction": direction, "lots": lots,
                                        "sl": sl, "tp": tp,
                                        "comment": comment or "ForexTrader",
                                    })
            if r.status_code not in (200, 201):
                try:
                    return r.json()
                except Exception:
                    return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
            return r.json()
        except Exception as e:
            return _send_failure(e)

    async def close_position(self, ticket: int) -> dict:
        if not self._url:
            return {"error": "MT5 bridge not configured"}
        try:
            r = await self._request("post", f"{self._url}/close/{ticket}", timeout=10.0)
            if r.status_code not in (200, 201):
                try:
                    return r.json()
                except Exception:
                    return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
            return r.json()
        except Exception as e:
            return _send_failure(e)

    async def partial_close(self, ticket: int, lots: float) -> dict:
        if not self._url:
            return {"error": "MT5 bridge not configured"}
        try:
            r = await self._request("post", f"{self._url}/partial-close/{ticket}",
                                    timeout=10.0, json={"lots": lots})
            if r.status_code not in (200, 201):
                try:
                    return r.json()
                except Exception:
                    return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
            return r.json()
        except Exception as e:
            return _send_failure(e)

    async def modify_order(self, ticket: int, sl: Optional[float], tp: Optional[float]) -> dict:
        if not self._url:
            return {"error": "MT5 bridge not configured"}
        try:
            r = await self._request("post", f"{self._url}/modify/{ticket}",
                                    timeout=10.0, json={"sl": sl, "tp": tp})
            if r.status_code not in (200, 201):
                try:
                    return r.json()
                except Exception:
                    return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
            return r.json()
        except Exception as e:
            return _send_failure(e)

    async def send_credentials(self, login: int, password: str, server: str) -> dict:
        if not self._url:
            return {"error": "MT5 bridge not configured"}
        try:
            r = await self._request("post", f"{self._url}/credentials", timeout=15.0,
                                    json={"login": login, "password": password, "server": server})
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    async def reconnect(self) -> dict:
        if not self._url:
            return {"error": "MT5 bridge not configured"}
        try:
            r = await self._request("post", f"{self._url}/reconnect", timeout=15.0)
            return r.json()
        except Exception as e:
            return {"error": str(e)}

    async def enable_autotrading(self) -> dict:
        """Ask the bridge to enable AutoTrading in the MT5 terminal via Win32."""
        if not self._url:
            return {"enabled": False, "error": "bridge not configured"}
        try:
            r = await self._request("post", f"{self._url}/enable_autotrading", timeout=15.0)
            return r.json()
        except Exception as e:
            return {"enabled": False, "error": str(e)}

    async def get_deal_history(self, days: int = 7) -> Optional[list[dict]]:
        if not self._url:
            return None      # not configured: we cannot look
        try:
            r = await self._request("get", f"{self._url}/history",
                                    timeout=12.0, params={"days": days})
            if r.status_code == 200:
                return r.json().get("history", [])
        except Exception:
            return None
        return None

    async def get_position_history(self, ticket: int) -> Optional[list[dict]]:
        if not self._url:
            return None      # not configured: we cannot look
        try:
            r = await self._request("get", f"{self._url}/history/position/{ticket}",
                                    timeout=12.0)
            if r.status_code == 200:
                return r.json().get("history", [])
        except Exception:
            return None
        return None

    async def get_tick_at(self, ts: float) -> Optional[dict]:
        if not self._url:
            return None
        try:
            r = await self._request("get", f"{self._url}/tick_at",
                                    timeout=8.0, params={"ts": ts})
            if r.status_code == 200:
                return r.json()
        except Exception:
            pass
        return None
