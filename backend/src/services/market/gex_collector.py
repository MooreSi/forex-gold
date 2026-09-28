"""Daily GLD option-chain snapshot, for a GEX study (docs/todo/009).

Once a weekday from 22:00 London (after the US close), on the research
loop's minute timer: the GLD chain for expiries within MAX_DAYS, stored per
strike, plus GEX, the flip level and the call and put walls, and gold's spot
from the running engine's bridge so those levels convert to XAUUSD.

**It collects and nothing else.** No signal, gate or order reads this. The
point is to build the history a free source cannot give (yfinance serves
only today's chain), so that whether GEX helps can be measured before
anything is allowed to act on it.

Limits, stated so nobody reads more into it than is there:
- GLD is a small slice of gold's options market; the deep hedging is in
  COMEX gold futures options, which are paid data.
- Open interest is end of day, so a snapshot is a daily figure.
- The ratio uses gold's price at snapshot time against GLD's last price,
  about an hour after GLD's close.
- The sign convention is an assumption, recorded on every row.

Runs only where the Reversal engine has a bridge (it needs gold's price);
that also keeps it off a remote node without a second node-role check on
the timer, whose one call is pinned elsewhere.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from backend.src.db import database as db_module
from backend.src.services.market import gex
from backend.src.services.market import gex_repo

log = logging.getLogger(__name__)

LAST_RUN_KEY = "gex_snapshot_last"
UNDERLYING = "GLD"
MAX_DAYS = 60
MAX_EXPIRIES = 30   # GLD lists daily expiries; 60 days needs ~20-25
SLOT_HOUR = 22
RETRY_S = 1800
ASSUMPTIONS = ("naive GEX: dealers long calls, short puts; Black-Scholes gamma, "
               f"r={gex.RISK_FREE}; dollar gamma per 1% move")

_last_attempt = 0.0
_UNSET = object()


def fetch_chain(now: Optional[datetime] = None) -> dict:
    """GLD's spot and near-dated chain from yfinance. Network; blocking."""
    import yfinance as yf
    now = now or datetime.now(timezone.utc)
    t = yf.Ticker(UNDERLYING)
    hist = t.history(period="5d")
    spot = float(hist["Close"].iloc[-1])
    rows, used = [], []
    for exp in t.options:
        expiry = datetime.strptime(exp, "%Y-%m-%d").replace(hour=20, tzinfo=timezone.utc)
        days = (expiry - now).total_seconds() / 86400
        if days <= 0 or days > MAX_DAYS:
            continue
        chain = t.option_chain(exp)
        by_strike: dict[float, dict] = {}
        for side, frame in (("call", chain.calls), ("put", chain.puts)):
            for _, r in frame.iterrows():
                k = float(r["strike"])
                row = by_strike.setdefault(k, {"expiry": exp, "strike": k, "t_years": days / 365,
                                               "call_oi": 0.0, "put_oi": 0.0,
                                               "call_iv": None, "put_iv": None})
                oi = r.get("openInterest")
                row[f"{side}_oi"] = float(oi) if oi == oi and oi is not None else 0.0
                iv = r.get("impliedVolatility")
                row[f"{side}_iv"] = float(iv) if iv == iv and iv is not None else None
        rows.extend(by_strike.values())
        used.append(exp)
        if len(used) >= MAX_EXPIRIES:
            break
    return {"spot": spot, "rows": rows, "expiries": used}


def _engine_bridge() -> Optional[Any]:
    try:
        from backend.src.services.reversal_engine import reversal_engine_service as _svc
        engine = _svc.get_instance()
        return getattr(engine, "_bridge", None) if engine else None
    except Exception:
        return None


def _snapshot(chain: dict, xau_spot: float, asof: str) -> tuple[dict, list[dict]]:
    spot = float(chain["spot"])
    rows = chain["rows"]
    s = gex.summarise(rows, spot)
    ratio = round(xau_spot / spot, 6) if spot > 0 else None
    snap = {
        "asof_date": asof, "underlying": UNDERLYING, "spot": spot,
        "xau_spot": xau_spot, "ratio": ratio, **{k: s[k] for k in
        ("total_gex", "flip_level", "call_wall", "put_wall", "n_rows")},
        "xau_flip_level": gex.to_xau(s["flip_level"], ratio),
        "xau_call_wall": gex.to_xau(s["call_wall"], ratio),
        "xau_put_wall": gex.to_xau(s["put_wall"], ratio),
        "expiries": json.dumps(chain.get("expiries") or []),
        "source": "yfinance", "assumptions": ASSUMPTIONS,
    }
    return snap, gex.per_strike(rows, spot)


async def gex_snapshot_sweep(engine: Any, now: Optional[datetime] = None,
                             fetcher: Optional[Callable[[], dict]] = None,
                             bridge: Any = _UNSET) -> None:
    """One check of the minute timer. At most one snapshot per weekday."""
    global _last_attempt
    now = now or datetime.now(ZoneInfo("Europe/London"))
    if now.weekday() >= 5 or now.hour < SLOT_HOUR:
        return
    asof = now.strftime("%Y-%m-%d")
    if db_module.get_app_config(LAST_RUN_KEY) == asof:
        return
    if now.timestamp() - _last_attempt < RETRY_S:
        return
    bridge = _engine_bridge() if bridge is _UNSET else bridge
    if bridge is None:
        return
    tick = await bridge.get_fresh_tick()
    xau = float(getattr(tick, "mid", 0) or 0) if tick is not None else 0.0
    if xau <= 0:
        return

    _last_attempt = now.timestamp()
    try:
        chain = await asyncio.to_thread(fetcher or fetch_chain)
    except Exception as e:
        log.warning("[GEX] %s chain unavailable, retrying in %d min: %s",
                    UNDERLYING, RETRY_S // 60, e)
        return
    if not chain or not chain.get("rows") or not chain.get("spot"):
        log.warning("[GEX] %s chain came back empty; retrying later", UNDERLYING)
        return

    snap, strikes = _snapshot(chain, xau, asof)
    gex_repo.create_schema()
    gex_repo.insert_snapshot(snap, strikes)
    db_module.set_app_config(LAST_RUN_KEY, asof)
    log.info("[GEX] %s %s: GEX %s, flip %s (XAU %s), walls %s/%s (XAU %s/%s), %d rows",
             UNDERLYING, asof, snap["total_gex"], snap["flip_level"], snap["xau_flip_level"],
             snap["put_wall"], snap["call_wall"], snap["xau_put_wall"], snap["xau_call_wall"],
             snap["n_rows"])
