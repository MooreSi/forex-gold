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
    trigger as _trigger,
)

log = logging.getLogger(__name__)

# The 4H is where the entry is taken and where the indicators are read.
ENTRY_TIMEFRAME = "H4"
ENTRY_LABEL = "4H"
DAILY_TIMEFRAME = "D1"
# The lowest of the three timeframes, and the one that says WHEN. Alex G's
# guide: "a 4:1 or 8:1 ratio of timeframes (e.g. Daily, 4H, 30-min) so you see
# the long-term trend and then zoom in for entries." The community's checklist
# names the same group, "2H, 1H, 30m", and gives it a quarter of the score.
TRIGGER_TIMEFRAME = "M30"
# Two hundred 30m bars is about four trading days -- enough for the swing
# points a shift of structure is measured against, and no more.
TRIGGER_COUNT = 200

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

# No ZONE_MERGE_ATR. Until 2026-09-21 two bands within 1.5 x ATR of each other
# were folded into one area of interest, because every swing point was a zone
# and the raw output was a wall of hairlines. The owner replaced the rule:
# three touches validate a key horizontal zone, and bands are one level when
# they OVERLAP -- never by an ATR proximity gap, which glues distinct levels
# together and manufactures the touch counts the validation reads. The wall of
# hairlines is now handled by refusing to call a one-touch band a level at
# all, which is a filter rather than a smear.

# How far from price an order may rest, in DAILY ATRs. Reported 2026-09-21: a
# resting order went out at a zone price would take days to reach, on a method
# whose entire premise is a week of planning ("2-3 hours weekly" is the pitch).
# `propose` was taking the nearest qualifying zone with no ceiling at all, and
# across several months of daily and weekly levels the nearest one below price
# can be hundreds of dollars away -- a perfectly real level, and not one this
# week's order belongs at.
#
# Measured in daily ATR, not points, because "how far away" only means anything
# as "how long would it take": 300 points is a fortnight on a quiet gold and two
# sessions on a violent one. Three days rather than five: price meanders, so a
# level three average days off is roughly a week of real travel, and a week is
# the horizon the method plans over. A reasoned number, not a measured one --
# docs/system/domains/trading/020-set-and-forget.md records it as open.
MAX_ENTRY_DAILY_ATR = 3.0

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
    trigger_candles = await engine.get_candles(
        TRIGGER_TIMEFRAME, TRIGGER_COUNT) or []
    weekly = resample.to_weekly(daily)

    price = float(entry[-1]["close"]) if entry else None
    closes = [float(c.get("close") or 0.0) for c in entry]
    entry_bias = structure.bias(entry)

    atr = _ict.atr(entry, ATR_PERIOD) if len(entry) > ATR_PERIOD else 0.0
    # The DAILY range, which is what decides whether a zone is worth resting an
    # order at. The 4H ATR answers "how wide is a bar"; this answers "how far
    # does gold go in a day", and only the second one converts a distance into
    # a wait.
    daily_atr = _ict.atr(daily, ATR_PERIOD) if len(daily) > ATR_PERIOD else 0.0

    # The major levels, marked on the HIGHER timeframes only and each found by
    # scanning backward just far enough to validate it. The 4H does not mark
    # levels: it is where execution happens, reacting to the ones the Daily
    # and the Weekly have already established. A 4H that marked its own would
    # put a level under every recent wick, which is the opposite of trading
    # the majors.
    #
    # Weekly and Daily are scanned separately and then folded together on
    # OVERLAP, because a weekly band and a daily band at the same price are
    # one level a trader would draw once -- and a level both timeframes agree
    # on is the strongest kind the method recognises.
    daily_zones, daily_bars = aoi.mark(daily, price) if daily and price else ([], 0)
    weekly_zones, weekly_bars = (aoi.mark(weekly, price)
                                 if weekly and price else ([], 0))
    zones = aoi.merge(daily_zones + weekly_zones)
    if len(zones) > aoi.DEFAULT_LIMIT:
        nearest = sorted(zones, key=lambda z: aoi.distance(z, price))
        zones = sorted(nearest[:aoi.DEFAULT_LIMIT], key=lambda z: z["low"])
    impulse = structure.last_impulse(entry, entry_bias) if entry else None

    return {
        "price": price,
        "weekly_bias": structure.bias(weekly),
        "daily_bias": structure.bias(daily),
        "entry_bias": entry_bias,
        "entry_timeframe": ENTRY_LABEL,
        "daily_atr": daily_atr,
        "zones": zones,
        # How much history the levels above actually came from. On screen so
        # that "does it really measure back to last September?" has an answer
        # without reading the source.
        "zone_scan": {"daily_bars": daily_bars, "weekly_bars": weekly_bars},
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
        # The 30m series the trigger is read from. `propose` evaluates it,
        # because the direction it needs is derived there and deriving it twice
        # is how the two stages end up disagreeing about which way the trade is.
        "trigger_candles": trigger_candles,
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
    # Only bands narrow enough to BE a level can carry an entry or a target.
    # Alex G's guide: "keep the zone reasonably narrow". A band wide enough to
    # swallow price makes "price is at an area of interest" trivially true, and
    # a take-profit inside one is a target with a thousand points of slack.
    #
    # Applied here rather than in `gather`: a wide band is still real structure
    # and worth drawing on the chart. It is not a precise enough level to place
    # an order at, which is a different claim.
    zones = aoi.within_width(evidence.get("zones") or [],
                             float(price or 0.0))
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

        # Near enough to be worth waiting for. An order resting where price
        # will not arrive for a fortnight is not a set-and-forget trade, it is
        # a bet left on the table -- and it looks identical on screen to one
        # that fills tomorrow.
        refusal = _out_of_reach(abs(price - entry), evidence, want_kind,
                                direction)
        if refusal:
            return None, refusal

    stop = _stop_for(direction, zone, evidence, tol)
    target_zone = aoi.next_opposing(zones, entry, direction)
    if target_zone is None:
        return None, ("There is no opposing area of interest to take profit "
                      "at, so the reward cannot be measured and there is no "
                      "way to know whether this clears 1:2. Set & Forget takes "
                      "profit at the next zone, not at a multiple of the risk.")
    target = target_zone["low"] if direction == "BUY" else target_zone["high"]

    # ── The three stages ────────────────────────────────────────────────────
    # Rebuilt 2026-09-21. The old model had two -- pick a zone, rest an order
    # at it -- and with nothing to wait for it committed immediately and let
    # the market come to it, which on a level a fortnight away is not a trade.
    #
    #   armed      the zone is chosen, price has not reached it
    #   waiting    price is at the zone, the 30m has not reacted
    #   triggered  price is at the zone AND the 30m has shifted or engulfed
    #
    # The first two are still returned, with their levels, so the plan is
    # visible before it is live. `setup.invalidations` is what makes them
    # unplaceable -- the same mechanism that refuses a thin ratio, so the
    # button is disabled with a reason rather than mysteriously.
    arrived = _trigger.has_arrived(zone, price, tol)
    fired = _trigger.evaluate(evidence.get("trigger_candles") or [], direction) \
        if arrived else None
    stage = "triggered" if fired else ("waiting" if arrived else "armed")

    if stage == "triggered":
        # The guide: "enter immediately after a signal candle closes". The
        # point of waiting for the 30m is that price is already AT the level
        # when it fires, so there is nothing left to rest an order for.
        entry = price

    candidate = setup.build(
        direction, entry, stop, target,
        order_type=setup.order_type_for(direction, entry, price, tol),
    )
    candidate["stage"] = stage
    candidate["trigger"] = fired
    candidate["zone"] = zone
    candidate["target_zone"] = target_zone
    # On the candidate rather than derived in the browser, so the page can say
    # "about two days away" beside the entry instead of leaving the operator to
    # judge it off the chart -- which is what went wrong on 2026-09-21.
    candidate["distance"] = abs(price - candidate["entry"])
    candidate["distance_days"] = _days_away(candidate["distance"], evidence)
    return candidate, ""


def _days_away(distance: float, evidence: dict) -> Optional[float]:
    """A distance as a number of average days, or None when it cannot be told.

    None, not zero: an unreadable daily range means the wait is UNKNOWN, and
    rendering that as "0 days" would read as "fills immediately" -- the most
    encouraging possible wrong answer.
    """
    daily_atr = float(evidence.get("daily_atr") or 0.0)
    if daily_atr <= 0:
        return None
    return distance / daily_atr


def _out_of_reach(distance: float, evidence: dict, kind: str,
                  direction: str) -> str:
    """Why this zone is too far to rest an order at, or "" if it is not.

    The reason names the distance AND the wait. "No setup" tells the operator
    nothing they can act on; "300 points below, about 15 days of movement"
    tells them whether to come back tomorrow or next month.

    An unreadable daily range applies no ceiling at all. Refusing everything
    because the bridge served no daily candles would read as a market with no
    setups in it rather than as missing data.
    """
    days = _days_away(distance, evidence)
    if days is None or days <= MAX_ENTRY_DAILY_ATR:
        return ""
    side = "below" if direction == "BUY" else "above"
    return (
        f"The nearest {kind} zone is {distance:.0f} points {side} price — "
        f"about {days:.0f} days of movement at gold's current daily range. "
        f"Set & Forget plans a week at a time, so an order resting that far "
        f"out is a bet left on the table rather than a trade. Nothing to place "
        f"yet; the level is still worth watching."
    )


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
        "model": _ai.active_model(cfg),
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
            if k not in ("candles", "weekly_candles", "daily_candles",
                         "trigger_candles")}
