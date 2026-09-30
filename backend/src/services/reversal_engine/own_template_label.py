"""Replay a signal through the EA template it traded under, or say why not.

docs/todo/reversal-engine/250, step 2. `tpl_label` replays every signal
through one approximation of "30 TP1 SL50 and Trail"
(`entry_study.template_policy`: one partial and a runner). On the Mac 551 of
963 executed signals traded under another template or a non-template
strategy, so for them the label described a trade nobody placed.

This hands the signal's own template to `backtest/template_simulator`, the
walk read line by line out of the EA's `ManageTemplate()`, and refuses
rather than approximates what that walk cannot model: grid mode, resting
legs, account-wide harvest, the staged and fractal trails, and dynamic-ATR
templates (no ATR is passed; the live path's ATR comes from DPM candles
this replay does not rebuild). A refusal is a reason string and no number.

The entry bar is walked on its ADVERSE side only, as `tpl_label` does: the
trigger fell somewhere inside it, and its favourable extreme may predate the
fill. The label is slightly pessimistic rather than flattering.

Pure: bars in, a label out. No database, no broker, no clock. Nothing here
places, closes or modifies a trade. Research only -- it does not feed
`tpl_r`, `edge_model` or the v9 model.

Known limit: templates keep no edit history, so a signal is replayed under
its template's definition at the time of the replay.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from backend.src.services.backtest import template_simulator as ts

BAR_S = 60.0
HORIZON_S = 6 * 3600.0          # the same horizon as entry_study / tpl_label

_PREFIX = "template:"


@dataclass(frozen=True)
class OwnLabel:
    r: Optional[float]
    refusal: str = ""
    exit: str = ""               # the simulator's outcome: sl | tp | timeout


def template_name(strategy: Optional[str]) -> Optional[str]:
    """"template:<name>" -> "<name>"; anything else is not a template."""
    s = str(strategy or "")
    return s[len(_PREFIX):] if s.startswith(_PREFIX) and len(s) > len(_PREFIX) else None


def _walk(bars: Sequence[dict], direction: str, entry: float,
          trigger_time: float) -> Optional[list[dict]]:
    """The bars from the trigger's own bar to the horizon, the first cut to
    its adverse side; None when no bar holds the trigger."""
    idx = None
    for i, b in enumerate(bars):
        if float(b["ts"]) <= trigger_time:
            idx = i
        else:
            break
    if idx is None or trigger_time - float(bars[idx]["ts"]) >= BAR_S:
        return None
    first = dict(bars[idx])
    if str(direction).upper() == "BUY":
        first["high"] = entry
    else:
        first["low"] = entry
    first["close"] = entry
    end = trigger_time + HORIZON_S
    return [first] + [dict(b) for b in bars[idx + 1:] if float(b["ts"]) <= end]


def label(template: Optional[dict], bars: Sequence[dict], direction: str,
          trigger_price: float, trigger_time: float,
          cost_pts: float) -> OwnLabel:
    """R after `cost_pts` (a round trip, in price) of `template`'s trade
    entered at `trigger_price` at `trigger_time`, or a refusal."""
    why = ts.unsupported_reason(template) if template else "no template"
    if why:
        return OwnLabel(None, why)
    stop = ts.pips_to_price(float(template.get("sl_pips") or 0.0))
    if stop <= 0:
        return OwnLabel(None, "template states no stop: R has no denominator")

    walk = _walk(bars, direction, float(trigger_price), float(trigger_time))
    if not walk:
        return OwnLabel(None, "no bars hold the trigger")

    res = ts.simulate(template, walk, float(trigger_price),
                      str(direction).upper() == "BUY")
    if res.lot_size <= 0:
        return OwnLabel(None, "template sized a zero lot")
    r = res.pnl_price / (res.lot_size * stop) - cost_pts / stop
    return OwnLabel(round(r, 4), "", res.outcome)
