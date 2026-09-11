"""Built-in EA template presets.

Section 2 of `docs/todo/reversal-engine/200`: the trade-management template
the reversal engine's own measurements argue for.

A NEW template rather than an edit of an existing one, so the change is A/B
observable rather than a silent retune of whatever channels already use. It
is installed on request and **bound to no channel**, so it trades nothing
until somebody selects it -- which, on a live account, is a demo session's
decision and not an upgrade's.

Units, because they are the easiest thing here to get wrong: `*_pips` fields
are EA pips and `PipsToPrice(p) = p * 10 * _Point`, so on XAUUSD (`_Point`
0.01) 10 pips is 1.0 in price. The reversal engine's mean 5.75 "point" stop
is therefore 57.5 pips.
"""
from __future__ import annotations

from backend.src.services.broker import ea_templates

PRESET_NAME = "Reversal ATR v1"

# Only the fields that differ from DEFAULTS, so a future default change is
# inherited rather than silently frozen at whatever it was today.
REVERSAL_ATR_V1: dict = {
    # ── Entry shape ──────────────────────────────────────────────────
    # Single entry. `backtest/template_simulator.py` refuses grid mode and
    # resting entries outright, so a grid template cannot be evaluated at
    # all -- and a grid doubles the exposure question while the payoff one
    # is still open.
    "mode": "single",
    "anchors": 1,
    "pendings": 0,

    # ── The fix for the inverted payoff ──────────────────────────────
    # The stop and the first target are the SAME ATR multiple, so R is
    # constant across regimes instead of varying 0.43 to 0.75 with level
    # score. A 1:1 first target at a 59.4% win rate is profitable; the
    # reference channel's fixed 3-point TP1 against a 4-7 point stop is not.
    #
    # 1.2 is provisional and says so. It is a starting value to be replaced
    # by the fitted MAE/MFE quantiles from `market/barrier_fit.py` as soon
    # as the excursion backfill has produced them.
    "use_dynamic_atr": True,
    "atr_period": 14,
    "atr_sl_mult": 1.2,
    "atr_tp1_mult": 1.2,
    # Without this, the stop and TP1 scale with volatility and TP2 does
    # not. See template_levels.atr_scaled_ladder.
    "atr_ladder_scale": True,

    # ── The ladder ───────────────────────────────────────────────────
    # Two rungs. 83% of executed signals never reach TP1 and 7% reach TP4,
    # so levels 3 to 8 are decoration -- and a decorated ladder makes the
    # pct column describe a position the trade never has.
    #
    # These pips values set the ladder's SHAPE only: atr_ladder_scale
    # rescales the whole thing so TP1 lands on its ATR multiple, and 100 /
    # 200 is simply "the runner is twice the first target".
    "tp1_pips": 100.0, "tp1_pct": 50.0,
    "tp2_pips": 200.0, "tp2_pct": 50.0,
    "partials": True,
    "close_full_on_last": True,

    # ── Breakeven, the single most important line here ───────────────
    # be_trigger is a TP LEVEL INDEX, not a distance. 1 means "move to
    # breakeven once TP1 has cleared", and TP1 books 50% when it does.
    # Item 030: 315 trades whose breakeven never moved returned +0.767R
    # against 126 that moved at +0.333R. Arming after a booked partial
    # cannot turn a winner into a scratch, because the winner has already
    # banked something.
    "be_mode": "entry_buffer",
    "be_buffer_pts": 1.0,
    "be_trigger": 1,

    # ── Trail ────────────────────────────────────────────────────────
    # `step`, not `staged`: the simulator refuses `staged` and `fractal`,
    # and a template the backtest cannot evaluate is a money change with no
    # evidence behind it. These distances are fixed pips and do NOT scale
    # with ATR -- the ladder scaling covers the targets, not the trail.
    "trail_mode": "step",
    "trail_activation": 120.0,
    "trail_distance": 60.0,
    "trail_step": 20.0,
    "trail_padding": 0.0,

    # ── Refusals ─────────────────────────────────────────────────────
    # Reject a signal whose own TP1:SL is upside down. Under today's
    # generator this rejects the entire strong-level cohort, which is the
    # point: it makes the inversion in section 1.1 impossible to ship
    # silently.
    "signal_rr_ratio": 0.9,
    # The template-level twin of reversal-engine/040, whose sub-five-minute
    # fill cohort lost $2,142. A fill well past its zone is the same adverse
    # selection in different coordinates. 30 pips = 3.0 in price.
    "late_guard_pips": 30.0,
    "anc_shave": True,

    # ── Left alone on purpose ────────────────────────────────────────
    # guard_pips / safety_cap_pips keep their defaults: they are what stop a
    # breakeven modification being rejected as an invalid stop, the failure
    # that cost a full -$100 on ticket 1663956102.
    #
    # harvest_pips MUST stay 0. It shipped as 1.0, silently overrode the
    # dollar threshold, and on 2026-08-26 closed two trades at $1.40 on a
    # template set to harvest at $30. It is not in the UI, so it could
    # neither be seen nor switched off.
    "harvest_pips": 0.0,
    "harvest_enabled": False,
    # Portfolio-level risk belongs to the risk governor. Duplicating it per
    # channel makes attribution impossible: two things closed the basket and
    # neither log says which.
    "equity_protect": 0.0,
    "basket_harvest_threshold": 0.0,
    # 0 = use the app's own risk_per_trade_pct sizing rather than a second,
    # competing sizing rule on the template.
    "risk_pct": 0.0,
    "auto_sl": True,
}

PRESETS: dict[str, dict] = {PRESET_NAME: REVERSAL_ATR_V1}


def install(name: str = PRESET_NAME, overwrite: bool = False) -> dict:
    """Save a built-in preset as a normal, editable template.

    Creates the row and nothing else. It is deliberately not wired to any
    selection, recommendation or default: a template bound to nothing trades
    nothing, and that separation is what makes shipping this safe on a live
    account.

    `overwrite=False` refuses to clobber an existing template of the same
    name, because that template may have been tuned since.
    """
    if name not in PRESETS:
        raise ValueError(f"no built-in preset named {name!r}")
    if not overwrite and ea_templates.get_ea_template(name) is not None:
        raise ValueError(
            f"a template named {name!r} already exists; it may have been "
            f"tuned since it was installed, so this refuses to replace it")
    return ea_templates.save_ea_template(name, dict(PRESETS[name]))
