"""What the other gate would have done with this Telegram signal (stage 1).

docs/todo/signal-validation/010. The sibling of
`reversal_engine/shadow.py`, deliberately: same champion/challenger shape,
same two rules, because both were learned there and neither is obvious.

  * **The champion must be in the table.** It is what actually happened. A
    list of challengers with nothing to compare against proves nothing.
  * **An unavailable fact abstains.** `would_take` is None, not False. A
    challenger that stood aside on a fact it never had would report a
    refusal rate that says nothing about the gate, and it would be read as
    though it did.

One rule is this module's own. **A challenger never takes what the live path
never offered.** These variants subtract gates from a decision; they cannot
add a fill that never happened, because there is no outcome to score it by
and inventing one would make every challenger look better than the champion
for free. A blocked decision is recorded as not-taken for every variant, and
what it measures is whether the gate would ALSO have blocked it.

THE BIAS IS THE ONE FACT THAT IS NOT FREE
-----------------------------------------
`decision_log.inline_facts` gathers the liquidity, event and spread facts on
the decision path because all three are free -- a clock read, an already
warm calendar, and a number the caller is holding. The H1 bias is not: it is
a bridge call, and the measured IME budget is 269 ms end to end with 256 ms
of that the broker POST.

So it is read here, in the background sweep, and **only while the decision
is still recent enough that the read is the same moment**. Past
`MAX_BIAS_LAG_S` the variant abstains rather than scoring a later market and
presenting it as the one the signal arrived in. The live gate's own bias
cache has a 60s TTL, so this is the tolerance the live path already accepts,
not a new one invented here.

THE ENTRY TRIGGER IS REPLAYED, NOT RE-READ
------------------------------------------
It reads M1 micro-structure, so it is only honest against the candles that
existed as of the decision instant. It is scored from
`bridge.get_candles_range(decided_at - window, decided_at)` -- the market as
it was, not as it is.

That makes it the opposite case to the bias: because the replay reconstructs
the moment, it has **no freshness limit**. A decision from last Tuesday is
exactly as scoreable as one from a minute ago, which is also what makes the
backfill in `decision_backfill` worth anything.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from backend.src.services.signals import decision_log_repo as _repo

log = logging.getLogger(__name__)

# The live path's own bias cache TTL is 60s (governor._HTF_BIAS_TTL_S), so a
# read inside this window is contemporaneous to the same tolerance the live
# gate already accepts. Doubled to survive a slow sweep, not to relax it.
MAX_BIAS_LAG_S = 120.0

# Points. The live IME path's own guard reads max_allowed_spread_points from
# fee settings; this is a fixed challenger so the variant means one thing
# across the whole study rather than moving when a setting does.
SPREAD_GUARD_POINTS = 30.0


@dataclass(frozen=True)
class Variant:
    name: str
    is_champion: bool = False
    respect_liquidity: bool = False
    respect_events: bool = False
    max_spread_points: Optional[float] = None
    respect_bias: bool = False
    respect_trigger: bool = False


DEFAULT_VARIANTS: tuple[Variant, ...] = (
    Variant("live (champion)", is_champion=True),
    Variant("session liquidity", respect_liquidity=True),
    Variant("event tier", respect_events=True),
    Variant("spread guard", max_spread_points=SPREAD_GUARD_POINTS),
    Variant("trend (HTF bias)", respect_bias=True),
    Variant("confirmed entry", respect_trigger=True),
)

# How far back the trigger replay reaches. entry_trigger.confirm's own
# checks look at a handful of bars; 30 M1 candles is the same window
# reversal_engine_live_execute asks for live, so the two see the same shape
# of market and a promoted gate behaves as its shadow said it would.
TRIGGER_REPLAY_BARS = 30


def decide(variant: Variant, row: dict) -> tuple[Optional[bool], str]:
    """`(would_take, reason)` for one variant over one recorded decision.

    `would_take` is None when the variant had no fact to judge by.
    """
    executed = bool(row.get("executed"))
    if variant.is_champion:
        return executed, "" if executed else str(row.get("skip_reason") or "blocked")

    if not executed:
        return False, "not executed by the live path"

    if variant.respect_liquidity:
        blocked = row.get("liquidity_blocked")
        if blocked is None:
            return None, "liquidity fact unavailable"
        if blocked:
            return False, "inside a rollover or reopen window"

    if variant.respect_events:
        blocked = row.get("event_blocked")
        if blocked is None:
            return None, "event fact unavailable"
        if blocked:
            return False, "inside a calendar event window"

    if variant.max_spread_points is not None:
        spread = row.get("spread_points")
        if spread is None:
            return None, "spread unavailable"
        if float(spread) > float(variant.max_spread_points):
            return False, (f"spread {float(spread):.1f} pts over "
                           f"{float(variant.max_spread_points):.0f}")

    if variant.respect_bias:
        blocked = row.get("bias_blocked")
        if blocked is None:
            return None, "bias unavailable"
        if blocked:
            return False, "against the higher-timeframe trend"

    if variant.respect_trigger:
        blocked = row.get("trigger_blocked")
        if blocked is None:
            return None, "entry trigger could not be evaluated"
        if blocked:
            return False, "entry not confirmed"

    return True, ""


def record_all(decision_id: int, row: dict,
               variants: tuple[Variant, ...] = DEFAULT_VARIANTS) -> None:
    """One row per variant. Never raises: this runs behind the sweep, and a
    measurement must never cost anything that matters."""
    if not decision_id:
        return
    for v in variants:
        try:
            take, reason = decide(v, row)
            _repo.insert_shadow(decision_id, v.name, take, reason)
        except Exception:
            log.debug("[DecisionShadow] %s failed", v.name, exc_info=True)


async def _bias_blocks(bridge: Any, direction: str) -> Optional[bool]:
    """True when the HTF bias gate WOULD block this direction, or None.

    The gate is forced on for the read. A shadow of a gate that is already
    switched on answers nothing, which is the whole reason this exists.
    """
    from backend.src.services.risk import governor as _gov
    forced = {"htf_bias_gate_enabled": 1}
    bias = await _gov.current_htf_bias(bridge, forced)
    if not bias:
        return None
    return _gov.htf_bias_blocks(direction or "", bias, forced) is not None


def _level_for(row: dict) -> Optional[float]:
    """The price the trigger confirms against.

    The entry MID when the signal stated a zone -- that is the level the
    channel actually named. The decision price otherwise, which is what an
    IME decision on a bare direction has instead. None when it has neither,
    and then the variant abstains: there is nothing to confirm.
    """
    try:
        lo, hi = row.get("entry_low"), row.get("entry_high")
        if lo is not None and hi is not None:
            return (float(lo) + float(hi)) / 2.0
        px = row.get("price")
        return None if px is None else float(px)
    except (TypeError, ValueError):
        return None


async def _trigger_blocks(bridge: Any, row: dict) -> Optional[bool]:
    """True when the entry trigger WOULD have refused this entry, or None.

    None on every failure path, deliberately, and for the reason
    `reversal_engine_live_execute._entry_trigger_blocks` gives about the
    live copy: a check that could not run is not a check that failed.

    The config is forced fully on -- both checks required -- because a
    shadow of a gate configured off answers nothing.
    """
    from backend.src.services.market import entry_trigger as _et

    level = _level_for(row)
    if level is None or level <= 0:
        return None

    decided_at = float(row.get("decided_at") or 0.0)
    if decided_at <= 0:
        return None

    candles = await bridge.get_candles_range(
        decided_at - TRIGGER_REPLAY_BARS * 60.0, decided_at, "M1")
    if not candles:
        return None

    from backend.src.services.reversal_engine.reversal_engine_service import (
        ReversalEngine as _RE,
    )
    # Reused rather than reimplemented: a second ATR would be a second
    # definition, and the duplicate-implementation gate exists because this
    # codebase has grown two of things before.
    atr = _RE._calc_atr(candles)

    cfg = _et.TriggerConfig(require_rejection=True, require_deceleration=True)
    result = _et.confirm(candles, level, str(row.get("direction") or "BUY"),
                         atr, cfg)
    return not result.passed


async def evaluate_pending(bridge: Any, limit: int = 200,
                           now: Optional[float] = None) -> int:
    """Score every decision that has not been scored yet. Returns the count.

    Each decision is isolated: a failure on one must not stop the sweep, and
    a failing fact within one must not cost that decision its other
    variants.
    """
    now_ts = time.time() if now is None else now
    pending = _repo.unevaluated_shadow(limit)
    for row in pending:
        try:
            row = dict(row)
            row["bias_blocked"] = None
            row["trigger_blocked"] = None
            executed = bool(row.get("executed"))
            fresh = (now_ts - float(row.get("decided_at") or 0.0)) <= MAX_BIAS_LAG_S
            if fresh and executed:
                try:
                    row["bias_blocked"] = await _bias_blocks(
                        bridge, str(row.get("direction") or ""))
                except Exception:
                    log.debug("[DecisionShadow] bias read failed", exc_info=True)
            # No freshness test: this one replays the moment rather than
            # reading the present, so age costs it nothing.
            if executed:
                try:
                    row["trigger_blocked"] = await _trigger_blocks(bridge, row)
                except Exception:
                    log.debug("[DecisionShadow] trigger replay failed", exc_info=True)
            record_all(int(row["id"]), row)
            _repo.mark_shadow_evaluated(int(row["id"]), now_ts)
        except Exception:
            log.debug("[DecisionShadow] evaluation failed", exc_info=True)
    return len(pending)


def report(variants: tuple[Variant, ...] = DEFAULT_VARIANTS) -> list[dict]:
    """Each variant's realised result over the trades it would have taken.

    `mean_r` is None, never 0.0, for a variant with nothing to score. Zero
    expectancy and no evidence are different statements, and a table that
    renders both as 0.000 invites the wrong one to be acted on -- the same
    reason reversal_engine/shadow.report holds this line.
    """
    rows = _repo.closed_shadow_decisions()
    acc: dict[str, dict] = {
        v.name: {"variant": v.name, "is_champion": v.is_champion,
                 "n_taken": 0, "n_skipped": 0, "n_abstained": 0,
                 "net": 0.0, "_r": 0.0, "_n_r": 0}
        for v in variants}

    for r in rows:
        bucket = acc.get(r["variant"])
        if bucket is None:
            continue
        take = r["would_take"]
        if take is None:
            bucket["n_abstained"] += 1
            continue
        if not take:
            bucket["n_skipped"] += 1
            continue
        bucket["n_taken"] += 1
        bucket["net"] += float(r["net_usd"] or 0.0)
        if r["realised_r"] is not None:
            bucket["_r"] += float(r["realised_r"])
            bucket["_n_r"] += 1

    out = []
    for b in acc.values():
        n_r = b.pop("_n_r")
        total_r = b.pop("_r")
        b["mean_r"] = (total_r / n_r) if n_r else None
        out.append(b)
    return out
