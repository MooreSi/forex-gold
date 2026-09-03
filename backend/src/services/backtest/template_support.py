"""Which EA templates the backtest can honestly simulate.

Option A of docs/todo/backtest/010, chosen by the owner 2026-09-03: simulate
the template fields that map cleanly onto a price walk, and refuse anything
else by name rather than approximating it.

The reason for refusing is not tidiness. The backtest's numbers pick the
templates that trade real money, and nothing compares a simulated template
against what `ManageTemplate()` actually does on the EA -- 287 lines, executed
per tick. An approximation produces a figure that looks authoritative and is
wrong, and the divergence is silent. "Not supported, because grid legs are not
modelled" is worth more than a number you would trust wrongly.

WHAT IS SIMULABLE

A single-mode template's management is a walk over prices: one market fill, a
stop, a TP ladder with partial closes, a breakeven step, and a trailing stop.
Every one of those is a comparison against the next bar or tick.

WHAT IS NOT, AND WHY

  mode=grid          several resting legs whose fills, sibling cancellation
                     and per-leg lots are their own execution model, not a
                     variant of one fill.
  pendings > 0       a resting entry order: whether it fills at all depends
                     on book behaviour this walk does not model. NOTE it is
                     `pendings` -- the COUNT -- that decides this, not
                     `pending_mode`, which is only "zone" or "step" and
                     describes how legs would be placed if there were any.
                     Every single-mode template in the owner's library
                     carries pending_mode="zone" with pendings=0; keying on
                     the mode refused all 22 of them.
  use_dynamic_atr    SL/TP derived from a live ATR the historical walk does
                     not reproduce tick-for-tick.
  equity_protect     acts on ACCOUNT equity across every open position. A
                     backtest of one signal has no account.
  harvest_enabled    same: closes on aggregate profit, not this trade's.

A field only excludes when it is ON. A template carrying them switched off is
perfectly simulable -- treating the mere presence of a key as unsupported
would refuse every template, since they all carry every key.
"""
from __future__ import annotations

from typing import Any, Optional

__all__ = ["can_simulate", "summarise", "UNSUPPORTED_WHEN_ON"]


def _on(value: Any) -> bool:
    """Is this field switched on?

    Templates store booleans as 0/1, strings as "off"/"on"/a mode name, and
    older rows can hold None. Only a value that is genuinely set counts.
    """
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "off", "none", "false")
    try:
        return float(value) != 0.0
    except (TypeError, ValueError):
        return bool(value)


# field -> why the walk cannot model it, in words that mean something beside
# the template's name in the UI.
UNSUPPORTED_WHEN_ON: dict[str, str] = {
    "use_dynamic_atr": (
        "use_dynamic_atr: stop and target are derived from a live ATR that "
        "the historical walk cannot reproduce tick-for-tick"
    ),
    "equity_protect": (
        "equity_protect: acts on total account equity across every open "
        "position, and a backtest of one signal has no account"
    ),
    "harvest_enabled": (
        "harvest_enabled: closes on aggregate profit rather than this "
        "trade's, so it has no meaning for a single simulated signal"
    ),
}

_PENDING_REASON = (
    "pendings: this template places resting entry orders, and whether one "
    "fills depends on order-book behaviour this backtest does not model"
)

_GRID_REASON = (
    "mode: grid templates place several resting legs with their own fills, "
    "lots and sibling cancellation -- a separate execution model, not a "
    "variant of the single market fill this backtest walks"
)


def can_simulate(template: Optional[dict]) -> tuple[bool, list[str]]:
    """(supported, reasons). Reasons are empty when supported.

    Every reason is listed, not just the first: fixing one field at a time
    to discover the next is a guessing game.
    """
    if not template:
        return False, ["no template was supplied"]

    reasons: list[str] = []

    if str(template.get("mode") or "single").strip().lower() == "grid":
        reasons.append(_GRID_REASON)

    # The COUNT of resting legs, not pending_mode -- see the module docstring.
    try:
        if float(template.get("pendings") or 0) > 0:
            reasons.append(_PENDING_REASON)
    except (TypeError, ValueError):
        pass

    for field, why in UNSUPPORTED_WHEN_ON.items():
        if _on(template.get(field)):
            reasons.append(why)

    return (not reasons), reasons


def summarise(templates: list) -> list[dict]:
    """One row per template for the backtest picker.

    Every template gets a row, including the unsupported ones. Omitting them
    would read as "this template does not exist" rather than "this template
    cannot be backtested, and here is why".
    """
    out: list[dict] = []
    for t in templates or []:
        ok, reasons = can_simulate(t)
        out.append({
            "name": (t or {}).get("name", ""),
            "supported": ok,
            "reasons": reasons,
        })
    return out
