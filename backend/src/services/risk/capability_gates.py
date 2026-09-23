"""One place that reads the new capability switches.

Every capability from `docs/todo/reversal-engine/200` ships behind its own
column in `vantage_risk_settings` (migration 41) and every one of them is
OFF. This module turns those columns into the config objects the pure
modules take, so there is a single answer to "is this on" rather than one
per call site -- which is how a gate ends up enabled in one path and not
another, and how nobody can say afterwards what was running.

Two properties are load-bearing and both are pinned by tests:

  * **A default settings row leaves everything inert.** Not "mostly
    inert": `sizing_inputs` out of a default row returns a `SizingInputs`
    that multiplies a lot size by exactly 1.0.
  * **A settings row MISSING these columns behaves the same.** A client
    that has not run migration 41 must trade exactly as it did, rather
    than crashing or silently enabling something.
"""
from __future__ import annotations

from typing import Optional

from backend.src.services.market import entry_trigger as _trigger
from backend.src.services.market import liquidity_map as _lmap
from backend.src.services.risk import event_tiers as _events
from backend.src.services.risk import session_liquidity as _liquidity
from backend.src.services.risk import sizing_policy as _sizing


def _on(rs: dict, key: str) -> bool:
    return bool(rs.get(key, 0))


def _num(rs: dict, key: str, default: float) -> float:
    try:
        v = rs.get(key)
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def atr_barrier_config(rs: dict) -> Optional[dict]:
    """Config for `signal_generator.atr_barriers`, or None when off.

    None rather than `{"enabled": False}` so a caller cannot accidentally
    pass a disabled config into something that only checks for presence.
    """
    if not _on(rs, "re_atr_barriers_enabled"):
        return None
    return {"enabled": True,
            "stop_mult": _num(rs, "re_atr_stop_mult", 1.2),
            "tp1_mult": _num(rs, "re_atr_tp1_mult", 1.2)}


def entry_trigger_config(rs: dict) -> Optional[_trigger.TriggerConfig]:
    """Config for `entry_trigger.confirm`, or None when off.

    An enabled gate with no checks selected is returned as a real config
    with nothing required, which confirms everything. That is what the
    switches say, and it is deliberately not reinterpreted as either "block
    everything" or "turn some checks on for them".
    """
    if not _on(rs, "entry_trigger_enabled"):
        return None
    return _trigger.TriggerConfig(
        require_rejection=_on(rs, "entry_trigger_rejection"),
        require_deceleration=_on(rs, "entry_trigger_deceleration"),
        max_range_ratio=_num(rs, "entry_trigger_max_range_ratio", 0.5),
    )


def meta_label_gate(rs: dict) -> tuple[bool, float]:
    """`(enabled, threshold)` for the meta-labeller."""
    return (_on(rs, "meta_label_gate_enabled"),
            _num(rs, "meta_label_threshold", 0.5))


def liquidity_map_enabled(rs: dict) -> bool:
    return _on(rs, "liquidity_map_levels_enabled")


def asian_bias_exempt(rs: dict, session: Optional[str]) -> bool:
    """Does `session` opt out of the higher-timeframe trend rule entirely?

    `governor.htf_bias_blocks` was measured over the whole clock. Split by
    session it inverts: in 00-07 UTC trades WITH the bias are 693 at -$6.26
    (CI [-9.40, -3.12], both chronological halves negative) and trades
    against it 621 at -$0.50 (CI straddles zero), while everywhere else
    against-the-bias is 1,154 at -$4.81 (CI [-7.48, -2.14]). Measured
    2026-09-12 over all 5,414 `re_signals` rows.

    So this stands the rule DOWN in Asia. It does not invert it: -$0.50 with
    an interval straddling zero is "no evidence of an edge", not an edge.

    Three conditions, and the middle one is load-bearing. **The trend gate
    must itself be on**, because the Reversal Engine's live path also
    carries the original `level_score < 0.75` counter-bias bypass
    (reversal-engine/090) and stands that down on this same answer. With the
    gate off that bypass is the only rule there is, and it predates this
    change by a month; exempting it here would switch off something nobody
    asked about.

    Session is compared to the exact spelling `level_detector.get_session`
    emits. A caller that cannot say what session it is in gets False, which
    is today's behaviour -- the same fail-closed direction as an unknown
    bias in `htf_bias_blocks`.

    Off by default (rules/60-adding-a-tunable), migration 45, and it has
    never been demoed.
    """
    if not _on(rs, "htf_bias_gate_enabled"):
        return False
    if not _on(rs, "htf_bias_asian_exempt"):
        return False
    return str(session or "").strip().lower() == "asian"


def liquidity_blocks(now_ts: float, rs: dict,
                     events: Optional[list] = None) -> Optional[str]:
    """A reason to stand aside on liquidity grounds, or None.

    Two independent switches behind one question, because the call site's
    question is one question. The clock-driven check runs first: "the week
    just opened" is more useful than "an event is near" when both are true.
    """
    if _on(rs, "session_liquidity_gate_enabled"):
        ok, reason = _liquidity.check(now_ts, _liquidity.Config())
        if not ok:
            return reason
    if _on(rs, "event_tier_gate_enabled"):
        ok, reason = _events.check(events or [], _events.Config())
        if not ok:
            return reason
    return None


def tg_event_tier_blocks(rs: dict, events: Optional[list] = None) -> Optional[str]:
    """A reason for the TELEGRAM path to stand aside near an event, or None.

    Stage 2 of docs/todo/signal-validation: the first gate promoted from the
    decision log's shadow record onto a path that can refuse a trade. Read
    over 322 scored decisions on 2026-09-21, the event tier stood aside from
    4 of the 39 it had an opinion about and avoided $219.50 of a $869.96
    loss on those rows -- a quarter of the loss for a tenth of the trades,
    and the only favourable ratio in the record that is not a blanket
    refusal. (`confirmed entry` refuses 99.4% of signals, which is a verdict
    on the channel rather than a filter; session liquidity and the spread
    guard changed nothing at all.)

    **Its own switch, not `event_tier_gate_enabled`.** That column is read by
    `liquidity_blocks` above, which the Reversal Engine's live path calls.
    Arming two engines from one switch destroys the attribution the staged
    plan exists for. The WINDOWS are shared -- `event_tiers.Config()`, the
    same default the shadow variant was scored against -- so there is one
    definition of what a tier-1 blackout is and two independent decisions
    about who honours it.

    **Off reads nothing.** `get_events` can reach the network, and a feed
    call on the order path for a gate nobody enabled is a cost with no
    benefit. It also matters that the switch is checked BEFORE the read: a
    gate that reads first is indistinguishable from one that is on, on any
    trace of the order path.

    **A calendar that could not be read does not block.** A feed that is down
    has not said no. That is the direction `upcoming_events` already fails,
    and the direction the shadow log ABSTAINS rather than refusing -- the
    alternative is a broken feed silently halting trading with nothing on any
    screen saying why.
    """
    if not _on(rs, "tg_event_tier_gate_enabled"):
        return None
    try:
        evs = _tg_events() if events is None else events
        ok, reason = _events.check(evs, _events.Config())
        return None if ok else reason
    except Exception:
        return None


def _tg_events() -> list:
    """Calendar events shaped for `event_tiers.check`.

    `news_calendar` caches the weekly feed to disk, so on the IME path -- 269
    ms end to end with 256 ms of it the broker POST -- this is a warm read,
    not a fetch. `mins_until` is derived here because the calendar stores
    absolute timestamps and the tier windows are relative.
    """
    import time as _time
    from backend.src.utils.news_calendar import get_events
    now = _time.time()
    return [dict(ev, mins_until=(float(ev.get("ts", 0)) - now) / 60.0)
            for ev in get_events()]


def sizing_inputs(rs: dict, atr: float, reference_atr: float,
                  drawdown_pct: float,
                  open_correlated_lots: float) -> _sizing.SizingInputs:
    """Inputs for `sizing_policy.apply`.

    The volatility and drawdown scalars share one switch, because they are
    one idea -- size to the risk actually being taken -- and splitting them
    would let a half-applied policy run without anybody choosing that. The
    correlated cap is independent: it is a hard ceiling rather than a
    scalar, and it is useful on its own.
    """
    scale_on = _on(rs, "vol_target_sizing_enabled")
    return _sizing.SizingInputs(
        atr=atr if scale_on else 0.0,
        reference_atr=reference_atr if scale_on else 0.0,
        drawdown_pct=drawdown_pct if scale_on else 0.0,
        open_correlated_lots=open_correlated_lots,
        correlated_cap_lots=_num(rs, "correlated_exposure_cap_lots", 0.0),
        # Outside `scale_on` on purpose: this is the account's ceiling, not
        # one of the capability's adjustments. `suggest_lot_size` already
        # applies it to the base lot, and it has to survive a policy whose
        # volatility scalar can multiply that base by up to 1.5.
        max_lots=_num(rs, "max_lot_size", 0.0),
    )


def xasset_features_enabled(rs: dict) -> bool:
    """Does the meta-labeller train and score with the cross-asset features?

    Decides nothing on its own: the live path consults the meta-labeller
    only when `meta_label_gate_enabled` is on. Migration 52, off by default.
    docs/todo/reversal-engine/230.
    """
    return _on(rs, "re_xasset_features_enabled")


def cme_context_enabled(rs: dict) -> bool:
    """Should the engine read CME futures context? Off, and inert either way.

    **Nothing consumes this yet, and that is deliberate.** There is no CME
    client, no entitlement and no ingest in this repo, so turning the switch
    on records the intent and changes no decision the engine makes. Same
    shape as `vol_target_sizing_enabled`, which has said so on the card
    since 2026-09-11.

    Why the switch exists at all: spot XAUUSD on this broker publishes
    bid/ask and no Last, so there is no trade side to read and "volume"
    everywhere in this system is tick volume -- a count of quote changes,
    not size. `services/market/order_flow.py` labels every result with the
    method that produced it for that reason. GC futures are the lit venue
    where gold prints real size, and the only route by which any of this
    becomes a measurement instead of a proxy.

    Why it stops here, and it is NOT cost: the data this would use --
    daily GC volume and open interest -- is published free by CME. Only
    real-time streaming is a paid entitlement and this engine has no use
    for it. What is unanswered is whether futures flow predicts anything
    about THESE trades, which nothing in this repo has measured, so
    building the ingest first would be building on a guess. Recorded in
    docs/simon-handover/039. Migration 46, off by default
    (rules/60-adding-a-tunable), never demoed.
    """
    return _on(rs, "re_cme_context_enabled")


# Every level type the detector can emit. A type missing from here is a
# type nobody can refuse, which is how a control silently stops covering
# the thing it was built for the next time a level source is added. The
# liquidity-map half is imported rather than restated for that reason.
KNOWN_LEVEL_TYPES: tuple[str, ...] = (
    "asia_low", "asia_high", "swing_high", "swing_low",
    "round_10", "round_5", "congestion", "unicorn",
) + tuple(_lmap.LEVEL_TYPES)


def blocked_level_types(rs: dict) -> set:
    """Level types the engine must not trade, from a comma-separated list.

    Empty by default, so nothing is refused until somebody names it.

    This exists because the first thing the live study measured that
    nothing in the app could act on was per-level-type outcome:
    `score_level` rates `round_5` highest of all at 0.78 and it is the
    worst cohort on the book (210 trades, -0.157R, -$1,323), because those
    weights were fitted against how often a Telegram channel fired near a
    level rather than against whether the trade made money.

    Refitting the weights changes every score in the system. Refusing a
    named type is the smaller, reversible move the evidence already
    supports, and it is a list the owner controls rather than a number a
    model moved.
    """
    raw = rs.get("re_blocked_level_types") or ""
    return {part.strip().lower() for part in str(raw).split(",") if part.strip()}
