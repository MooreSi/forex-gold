"""The orchestrator: evidence in, a candidate trade out, then a model's review.

Three properties hold this together, and all three are about refusing.

**The deterministic candidate is the floor, not the ceiling.** It is built from
the rules before any model is asked anything. That keeps the page useful and
honest with no API key configured, keeps the measured evidence free to look at,
and -- the reason it matters most -- means there is always something to check
the model's answer against.

**A model's numbers are re-validated, never adopted.** An LLM asked for a stop
and a target produces a stop and a target every single time, including for a
chart with nothing on it. Everything that comes back goes through
`setup.build` and `setup.invalidations`; what fails is discarded, the rules'
levels stand, and the page is told which rule it broke.

**Nothing here places anything.** `evaluate` returns a proposal. The browser
hands it to the existing money endpoints in `api/routers/orders.py` once the
operator has read it and pressed a button.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

from backend.src.services.ai import provider as _ai
from backend.src.services.positions import core_indicators as _ind
from backend.src.services.reversal_engine import ict_patterns as _ict
from backend.src.services.setforget import (
    aoi, confluence, patterns, prompt as _prompt, resample, setup, structure,
)

log = logging.getLogger(__name__)

# The 4H is where the entry is taken and where the indicators are read.
ENTRY_TIMEFRAME = "H4"
ENTRY_LABEL = "4H"
DAILY_TIMEFRAME = "D1"

# 400 4H bars is roughly ten weeks -- enough for an EMA 200 to mean something.
ENTRY_COUNT = 400
# 400 daily bars is roughly 80 trading weeks, which the weekly aggregation
# needs: a weekly structure read wants a couple of years, not a couple of
# months.
DAILY_COUNT = 400

EMA_FAST = 50
EMA_SLOW = 200
RSI_PERIOD = 14
ATR_PERIOD = 14

# How far beyond the zone (or the confirmation candle's tail) the stop sits, as
# a fraction of ATR. Not zero: a stop exactly on the level is taken out by the
# same wick that confirms it. Not large either -- the distance is the risk.
STOP_BUFFER_ATR = 0.25

# Two bands within this much ATR of each other are one area of interest. See
# `aoi.merge` -- without it the detector's output is unusable at this window
# size, and the failure looks like "the method never finds anything".
ZONE_MERGE_ATR = 1.5

# When ATR cannot be read -- a flat or empty series -- the zone tolerance falls
# back to a tenth of a percent of price, about $2 on gold at $2,000.
_FALLBACK_TOLERANCE_PCT = 0.001


async def gather(engine: Any) -> dict:
    """Everything measurable about the chart. No model, nothing billed.

    Its own function on purpose: the zones, the checklist and the candidate are
    the answer most of the time, and reading them should not cost anything. A
    page that could only show them by billing an API call would make every
    glance billable.
    """
    daily = await engine.get_candles(DAILY_TIMEFRAME, DAILY_COUNT) or []
    entry = await engine.get_candles(ENTRY_TIMEFRAME, ENTRY_COUNT) or []
    weekly = resample.to_weekly(daily)

    price = float(entry[-1]["close"]) if entry else None
    closes = [float(c.get("close") or 0.0) for c in entry]
    entry_bias = structure.bias(entry)

    atr = _ict.atr(entry, ATR_PERIOD) if len(entry) > ATR_PERIOD else 0.0
    # Bands closer together than this are one level. Without it a 400-bar
    # window yields a dozen hairlines, the next opposing zone sits a point or
    # two from every entry, and the section refuses every setup it ever finds
    # for a reason that is about the detector rather than about the chart.
    gap = atr * ZONE_MERGE_ATR

    # Daily levels and 4H levels together, merged. A trader's chart carries
    # both: the daily zone is why the trade exists and the 4H one is where the
    # order goes. Kept separate they would double-count the same band.
    zones = aoi.merge(
        aoi.zones(daily, reference=price, gap=gap)
        + aoi.zones(entry, reference=price, gap=gap),
        gap=gap,
    ) if entry or daily else []
    impulse = structure.last_impulse(entry, entry_bias) if entry else None

    return {
        "price": price,
        "weekly_bias": structure.bias(weekly),
        "daily_bias": structure.bias(daily),
        "entry_bias": entry_bias,
        "entry_timeframe": ENTRY_LABEL,
        "zones": zones,
        "atr": atr,
        "ema_fast": _ind.ema_last(closes, EMA_FAST) if closes else None,
        "ema_slow": _ind.ema_last(closes, EMA_SLOW) if closes else None,
        "rsi": _ind.rsi_last(closes, RSI_PERIOD) if closes else None,
        "confirmation": patterns.confirmation(entry),
        "impulse": impulse,
        "fib": confluence.retracement(impulse, price) if price else None,
        # The band the chart draws, priced here rather than in the browser:
        # `retracement_price` is the inverse of the function `fib` above was
        # scored with, so the picture and the score cannot disagree about where
        # 61.8% is. Empty when there is no completed leg -- a list of nulls
        # would be drawn as a band at zero, across the bottom of the chart,
        # looking like a real level nobody can account for.
        "fib_levels": _fib_levels(impulse),
        "candles": entry,
        "weekly_candles": weekly,
        "daily_candles": daily,
    }


def score(evidence: dict, candidate: Optional[dict]) -> dict:
    """The confluence checklist for a candidate, against this evidence.

    The merge lives here rather than in the controller or the router because
    it needs to know the shape of both dicts -- which zone a candidate was
    built at, and which direction to read every item for. A caller doing it
    would be a second place that knows, and controllers may not hold logic.

    With no candidate the checklist is still scored, for BUY: the items and
    their reasons are the useful part of a "no setup" page, and an empty panel
    beside "no setup" reads as a broken feature rather than a waiting one.
    """
    return confluence.score({
        **evidence,
        "direction": (candidate or {}).get("direction", "BUY"),
        "at_zone": (candidate or {}).get("zone"),
    })


def _fib_levels(impulse: Optional[dict]) -> list[dict]:
    """The retracement levels the chart draws, as {ratio, price} pairs."""
    out = []
    for ratio in confluence.LEVELS:
        price = confluence.retracement_price(impulse, ratio)
        if price is not None:
            out.append({"ratio": ratio, "price": float(price)})
    return out


def tolerance(evidence: dict) -> float:
    """How close to a zone counts as being at it.

    ATR, because what counts as close is a property of how far the instrument
    moves in a bar, not a number anyone should be choosing per screen.
    """
    atr = float(evidence.get("atr") or 0.0)
    if atr > 0:
        return atr
    price = float(evidence.get("price") or 0.0)
    return price * _FALLBACK_TOLERANCE_PCT


def propose(evidence: dict) -> tuple[Optional[dict], str]:
    """The candidate the rules produce, or None and the reason there is none.

    The reason is the product when there is no trade. "No setup" on a screen
    the operator has just pressed a button on is indistinguishable from a
    broken page; "the Weekly is bullish and the Daily is bearish, so the pair
    is too noisy" is the method working.
    """
    weekly, daily = evidence.get("weekly_bias"), evidence.get("daily_bias")
    price = evidence.get("price")
    if price is None:
        return None, ("No price is available — the bridge returned no candles. "
                      "Check the MT5 connection.")
    if weekly != daily:
        return None, (f"The Weekly ({weekly}) and the Daily ({daily}) disagree. "
                      f"Set & Forget skips a pair whose higher timeframes are "
                      f"fighting — it is too noisy to trade.")
    if weekly not in ("bullish", "bearish"):
        return None, (f"The Weekly and Daily are both {weekly}. There is no "
                      f"higher-timeframe direction to trade with, so there is "
                      f"no setup — this is a wait, not a failure.")

    direction = "BUY" if weekly == "bullish" else "SELL"
    want_kind = "demand" if direction == "BUY" else "supply"
    zones = evidence.get("zones") or []
    tol = tolerance(evidence)

    here = aoi.at_price(zones, price, want_kind, tolerance=tol)
    if here is not None:
        # Price is already at the zone: this is a market entry.
        zone, entry = here, price
    else:
        # The set-and-forget entry proper: rest an order at the nearest zone
        # price would come back to, and wait.
        candidates = [z for z in zones if z["kind"] == want_kind
                      and (z["high"] < price if direction == "BUY"
                           else z["low"] > price)]
        if not candidates:
            return None, (f"There is no {want_kind} zone "
                          f"{'below' if direction == 'BUY' else 'above'} price "
                          f"to rest an order at. Set & Forget enters at a zone "
                          f"and nowhere else.")
        zone = (max(candidates, key=lambda z: z["high"]) if direction == "BUY"
                else min(candidates, key=lambda z: z["low"]))
        # The proximal edge -- the first price that touches the zone, so the
        # order fills on the first tap rather than needing the zone eaten.
        entry = zone["high"] if direction == "BUY" else zone["low"]

    stop = _stop_for(direction, zone, evidence, tol)
    target_zone = aoi.next_opposing(zones, entry, direction)
    if target_zone is None:
        return None, ("There is no opposing area of interest to take profit "
                      "at, so the reward cannot be measured and there is no "
                      "way to know whether this clears 1:2. Set & Forget takes "
                      "profit at the next zone, not at a multiple of the risk.")
    target = target_zone["low"] if direction == "BUY" else target_zone["high"]

    candidate = setup.build(
        direction, entry, stop, target,
        order_type=setup.order_type_for(direction, entry, price, tol),
    )
    candidate["zone"] = zone
    candidate["target_zone"] = target_zone
    return candidate, ""


def _stop_for(direction: str, zone: dict, evidence: dict, tol: float) -> float:
    """Beyond the zone, or beyond the confirmation candle's tail if that is
    further out.

    Alex G measures from the confirmation candle -- "below the tail of the pin
    bar" -- and the zone is the fallback when no bar has confirmed yet. Taking
    whichever is further from the entry means the stop is outside BOTH, which
    is the only version that survives the wick that makes the setup.
    """
    buffer = max(tol * STOP_BUFFER_ATR, 0.0)
    found = evidence.get("confirmation")
    want = "bullish" if direction == "BUY" else "bearish"

    if direction == "BUY":
        level = zone["low"]
        if found and found.get("direction") == want and "low" in found:
            level = min(level, float(found["low"]))
        return level - buffer
    level = zone["high"]
    if found and found.get("direction") == want and "high" in found:
        level = max(level, float(found["high"]))
    return level + buffer


def _parse(raw: str) -> dict:
    r"""The model's reply as an object, however it chose to wrap it.

    `services/ai/json_reply` does the reading: same tolerance for a fence and
    for prose either side, but brace-matched rather than `re.search(r"\{.*\}")`,
    which is greedy and swallowed any trailing `}` in the apology after the
    object.
    """
    from backend.src.services.ai import json_reply
    return json_reply.parse_json_object(raw, "setforget_analysis")

def _review_levels(reply: dict, candidate: dict, price: float,
                   tol: float) -> tuple[Optional[dict], list[str]]:
    """The model's levels, rebuilt and re-validated, or None and the reasons.

    This is the gate. Nothing the model says about entries, stops or targets
    reaches a button without passing the same rules the deterministic candidate
    passed.
    """
    if reply.get("verdict") == "skip":
        return None, []
    levels = [reply.get("entry"), reply.get("stop_loss"), reply.get("take_profit")]
    if any(not isinstance(v, (int, float)) for v in levels):
        return None, []

    entry, stop, target = (float(v) for v in levels)
    revised = setup.build(
        candidate["direction"], entry, stop, target,
        order_type=setup.order_type_for(candidate["direction"], entry, price, tol),
    )
    reasons = setup.invalidations(revised)
    if reasons:
        return None, reasons
    revised["zone"] = candidate.get("zone")
    revised["target_zone"] = candidate.get("target_zone")
    return revised, []


async def evaluate(engine: Any, cfg: dict, timeout: int = 60) -> dict:
    """Read the chart, propose a trade, and have the configured model judge it.

    The model is only asked when there is something to judge. No candidate
    means no review: paying for a model to be told what the rules already said
    is money for nothing, and the reason is already on screen.
    """
    evidence = await gather(engine)
    candidate, why = propose(evidence)
    result = {
        "generated_at": time.time(),
        "price": evidence["price"],
        "evidence": _public(evidence),
        "candidate": candidate,
        "no_setup_reason": why,
        "confluence": score(evidence, candidate),
        "ai": None,
        "billed": False,
        "invalidations": setup.invalidations(candidate) if candidate else [],
    }
    if candidate is None or not _ai.is_configured(cfg):
        return result

    try:
        raw = await _ai.complete(
            cfg, _prompt.SYSTEM, _prompt.render(evidence, candidate),
            _prompt.MAX_TOKENS, timeout=timeout,
        )
    except Exception as exc:
        log.warning("[setforget] the provider did not answer: %s", exc)
        result["ai"] = {"error": f"The AI provider did not answer: {exc}",
                        "verdict": None}
        return result

    result["billed"] = True
    try:
        reply = _parse(raw)
    except Exception as exc:
        log.warning("[setforget] could not parse the model's reply: %s", exc)
        result["ai"] = {"error": "The model's reply was not the JSON object it "
                                 "was asked for, so its levels were not used.",
                        "verdict": None, "raw": raw[:500]}
        return result

    revised, rejected = _review_levels(
        reply, candidate, float(evidence["price"]), tolerance(evidence))
    if revised is not None:
        result["candidate"] = revised
        result["invalidations"] = setup.invalidations(revised)

    result["ai"] = {
        "verdict": str(reply.get("verdict") or "").lower() or None,
        "reasoning": str(reply.get("reasoning") or ""),
        "risks": str(reply.get("risks") or ""),
        "levels_rejected": rejected,
        "model": cfg.get("claude_model") or cfg.get("deepseek_model") or "",
        "error": None,
    }
    return result


def _public(evidence: dict) -> dict:
    """The evidence minus the candle series.

    The browser fetches its own candles for the chart from `/api/chart`, at the
    window it is drawing. Shipping a second copy here would put two series on
    one page that can disagree about what the last bar was.
    """
    return {k: v for k, v in evidence.items()
            if k not in ("candles", "weekly_candles", "daily_candles")}
