"""Macro context features for the Reversal Engine ML vector.

Spec: `docs/todo/001-reversal-macro-context.md`.

The Reversal Engine's 33-feature vector was built entirely from XAUUSD price
structure, the session clock, news proximity and reference-channel behaviour.
It had no view of the dollar, real yields or gold-specific volatility -- the
variables that decide whether a technically clean level holds or gets run
through. Bounce has consumed all five of these since it was written and
Breakout three of them since its v15; this module is the Reversal Engine's
read of the same source, `services/test_signal/market_context.py`.

Why this is a separate module rather than more lines in `ml_engine.py`: that
file sits at 787 lines against the structure gate's `LOC_CEILING = 800` and is
not in `structure_baseline.json`, so its ceiling cannot be raised. The gate
counts raw lines, comments included.

**Normalisation happens here, not in the caller.** The breakout engine passes
`dxy_momentum` and `tip_momentum` through raw and divides `gvz_level` by 40 at
the call site, ignoring the other two. The Reversal model fits an
SGDRegressor alongside LightGBM, and SGD is scale-sensitive: a raw VIX of 20
sitting next to a `dxy_momentum` of 0.03 dominates the gradient on scale
alone. Every value here is put on [0,1] or [-1,+1].

**`MACRO_NEUTRAL` therefore holds NORMALISED values.** `ml_engine` merges it
into `_FEATURE_NEUTRAL`, which right-pads the ~576 stored 33-wide vectors up
to the current width. Raw units there would tell the model the ten-year sat
off the top of the scale for every historical signal -- the same class of
silent fiction the v5 label rewrite exists to correct.
"""
from __future__ import annotations

import asyncio
import logging
import time

from backend.src.services.test_signal import market_context

_log = logging.getLogger("reversal_engine")

MACRO_FEATURE_NAMES = [
    "dxy_momentum",   # DXY 1h return [-1,+1]; negative = USD weakening = gold tailwind
    "us10y_level",    # US 10-year nominal yield / 6.0, clamped [0,1]
    "vix_level",      # CBOE VIX / 40.0, clamped [0,1]
    "gvz_level",      # CBOE Gold Volatility Index / 40.0, clamped [0,1]
    "tip_momentum",   # TIP ETF 1h return [-1,+1]; rising = real yields falling = gold tailwind
]

# (divisor, low, high) per feature. The divisor is 1.0 where market_context
# already returns a normalised value.
_SCALE = {
    "dxy_momentum": (1.0,  -1.0, 1.0),
    "us10y_level":  (6.0,   0.0, 1.0),
    "vix_level":    (40.0,  0.0, 1.0),
    "gvz_level":    (40.0,  0.0, 1.0),
    "tip_momentum": (1.0,  -1.0, 1.0),
}


def _normalise(name: str, raw: float) -> float:
    divisor, low, high = _SCALE[name]
    return max(low, min(high, raw / divisor))


# Normalised forms of market_context._NEUTRAL, derived rather than restated so
# the two cannot drift apart.
MACRO_NEUTRAL = {
    name: _normalise(name, float(market_context._NEUTRAL[name]))
    for name in MACRO_FEATURE_NAMES
}

# How often the macro series are actually worth re-reading. market_context
# caches for 15 minutes of its own, so this mainly keeps the blocking call out
# of the per-candidate path -- see get_cycle_context.
_REFRESH_S = 15 * 60

_ctx_cache: dict = {}
_ctx_ts: float = 0.0


def macro_features(signal_data: dict, ctx: dict | None) -> list[float]:
    """The five normalised macro features, in `MACRO_FEATURE_NAMES` order.

    `signal_data` wins over `ctx` so a stored signal re-scored at fill time is
    read against the conditions it was created under. Absent or unreadable
    values fall back to their neutral, never raise: `yfinance` is an optional
    import and a signal must still be generated without it.

    Reads are explicit `is None` checks, not the `x or y or default` idiom the
    breakout engine uses -- that swaps a genuine 0.0 for the default, and a
    ten-year of 0.0 is a long way from the 4.5% neutral.
    """
    out: list[float] = []
    ctx = ctx or {}
    for name in MACRO_FEATURE_NAMES:
        raw = signal_data.get(name)
        if raw is None:
            raw = ctx.get(name)
        if raw is None:
            out.append(MACRO_NEUTRAL[name])
            continue
        try:
            out.append(_normalise(name, float(raw)))
        except (TypeError, ValueError):
            out.append(MACRO_NEUTRAL[name])
    return out


async def get_cycle_context() -> dict:
    """The raw macro context, fetched at most once per `_REFRESH_S`.

    Called once per engine cycle above the candidate loop, never per
    candidate. Two reasons it is async and thread-offloaded:

      * `market_context.get_context()` is blocking HTTP. The Reversal cycle
        shares its event loop with position management, so stalling it is not
        cosmetic.
      * the Reversal cycle is `_CYCLE_INTERVAL_S = 60`, so without a window of
        its own this would reach for the wire every minute.

    On failure it returns the last good context if there is one, else `{}` --
    which `macro_features` reads as "use the neutrals". A fetch that fails must
    not throw away a context that is otherwise still usable.
    """
    global _ctx_cache, _ctx_ts
    now = time.time()
    if _ctx_cache and (now - _ctx_ts) < _REFRESH_S:
        return _ctx_cache
    try:
        ctx = await asyncio.to_thread(market_context.get_context)
        if ctx:
            _ctx_cache = ctx
            _ctx_ts = now
    except Exception as exc:
        _log.debug("[RE-Macro] context fetch failed: %s", exc)
    return _ctx_cache
