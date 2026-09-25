"""Which number sizes a trade: the one place that decides it.

docs/todo/risk/010. Trading > Risk > Per trade holds EITHER a risk % OR a
fixed lot size, never both (see `sizing_mode`). The "EA template override"
(`global_sizing_override`) makes every automated trade use it instead of the
EA template's own lots and risk %. ORB and Set & Forget size themselves and
never call this; neither does a lot a person typed.

Before this, the choice was hidden precedence copied into five order paths:
a global fixed lot above 0 silently beat Risk per trade %, and a template's
anchor lot was capped by Maximum lot size without checking the result. On
2026-09-24 that cap was 0, and Gold Diggers VIP sent MT5 two orders for 0
lots (`EA rejected template order: invalid volume`). The queued path treated
the same 0 as "use risk %", so the two routes disagreed about one signal.

This module decides the size; `fees_sizing.suggest_lot_size` is still the only
risk-% arithmetic, passed in so every caller keeps its own (faked in tests).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

MODE_RISK = "risk"
MODE_LOTS = "lots"

MIN_LOT = 0.01
# The schema's own default for max_lot_size, used where a row lacks the column.
_DEFAULT_MAX_LOT = 0.10

SuggestFn = Callable[[float, float, float, float], float]


class UnplaceableLot(ValueError):
    """A trade sized to less than the broker's minimum lot. Refused before it
    reaches the EA, with the setting that caused it."""


@dataclass(frozen=True)
class SizedLot:
    lot: float     # per leg
    fixed: bool    # a deliberate fixed value: exempt from the channel multiplier
    source: str    # for the log line


def _num(rs: Mapping, key: str, default: float = 0.0) -> float:
    try:
        return float(rs.get(key) if rs.get(key) is not None else default)
    except (TypeError, ValueError):
        return default


def sizing_mode(rs: Mapping) -> str:
    """Risk % or Fixed lots -- one or the other by construction.

    Fixed lots is live exactly when `strategy_lot_size` is above 0, which is
    what every order path has always read it as. Choosing Risk % on the Risk
    tab stores 0 there and keeps the lot in `strategy_lot_size_parked`, so
    the greyed-out field still shows it and switching back restores it.
    There is deliberately no separate mode column: a mode that could
    disagree with the lot is how a Risk % that looks set sizes nothing.
    """
    return MODE_LOTS if _num(rs, "strategy_lot_size") > 0 else MODE_RISK


def override_on(rs: Mapping) -> bool:
    """Does the global per-trade size replace the EA template's own?"""
    try:
        return bool(int(rs.get("global_sizing_override") or 0))
    except (TypeError, ValueError):
        return False


def global_fixed_lot(rs: Mapping) -> float:
    """The Fixed lots value when that mode is live, else 0 (size by risk %)."""
    return _num(rs, "strategy_lot_size") if sizing_mode(rs) == MODE_LOTS else 0.0


def max_lot(rs: Mapping) -> float:
    return _num(rs, "max_lot_size", _DEFAULT_MAX_LOT)


def global_risk_pct(rs: Mapping) -> float:
    return _num(rs, "risk_per_trade_pct", 0.5)


def template_leg_count(template: Mapping) -> int:
    """How many positions one signal opens. Single mode opens one whatever its
    pendings field says; a grid opens every anchor and pending leg."""
    if template.get("mode") != "grid":
        return 1
    legs = int(template.get("anchors") or 0) + int(template.get("pendings") or 0)
    return max(1, legs)


def template_lot(rs: Mapping, template: Mapping, entry: float, stop_loss: float,
                 balance: float, suggest_fn: SuggestFn) -> SizedLot:
    """The lot for ONE leg of a trade on this EA template.

    Override on: the global per-trade size. Fixed lots is per leg; Risk % is
    the total for the signal, split evenly across the legs (owner,
    2026-09-25), so 2% means 2% whether the template opens one leg or four.

    Override off: the template decides, exactly as before -- its own risk %
    when set, otherwise its anchor lot capped by Maximum lot size.

    The result can be 0 (a Maximum lot size of 0). That is deliberate: this
    reports the size, and `refuse_unplaceable` refuses it with the reason.
    """
    if override_on(rs):
        fixed = global_fixed_lot(rs)
        if fixed > 0:
            return SizedLot(min(fixed, max_lot(rs)), True, "global fixed lots")
        legs = template_leg_count(template)
        pct = global_risk_pct(rs) / legs
        return SizedLot(suggest_fn(entry, stop_loss, balance, pct), False,
                        f"global risk {pct:g}% per leg x {legs}")

    tpl_risk = _num(template, "risk_pct")
    if tpl_risk > 0:
        return SizedLot(suggest_fn(entry, stop_loss, balance, tpl_risk), False,
                        f"template risk {tpl_risk:g}%")
    anchor = _num(template, "lot_anchor") or MIN_LOT
    return SizedLot(min(anchor, max_lot(rs)), True, "template lots")


def template_sizes_its_own_legs(rs: Mapping, template: Mapping) -> bool:
    """Does the EA stage this template's legs at the template's own
    lot_anchor / lot_pending?

    Only when the template is the source of the size AND that size is its
    fixed lots. Otherwise the copy sent to the EA must carry the computed
    lot, because the EA prefers a non-zero template lot over the one sent.
    """
    return not override_on(rs) and _num(template, "risk_pct") <= 0


def ea_template_for_lot(rs: Mapping, template: dict, lot: float) -> dict:
    """The template as it should be SENT to the EA for a trade sized `lot`.

    HandleOpenTemplateGrid stages each leg at tpl_lot_anchor / tpl_lot_pending
    and only uses the lot it is sent when those are 0. When the size came from
    anywhere but the template's own fixed lots, the copy carries the computed
    lot on every leg -- otherwise the override, or a template's own risk %,
    would do nothing on a grid. The stored template is never touched.
    """
    if template_sizes_its_own_legs(rs, template):
        return template
    return dict(template, lot_anchor=lot, lot_pending=lot)


def refuse_unplaceable(lot: float, rs: Mapping) -> None:
    """Refuse a lot the broker would reject, before it reaches the EA."""
    if lot is not None and lot >= MIN_LOT:
        return
    if max_lot(rs) < MIN_LOT:
        raise UnplaceableLot(
            f"Trade sized to {lot or 0:g} lots because Maximum lot size is "
            f"{max_lot(rs):g} on Trading > Risk. Set it to at least {MIN_LOT}.")
    raise UnplaceableLot(
        f"Trade sized to {lot or 0:g} lots, below the broker minimum of "
        f"{MIN_LOT}. Check the per-trade size on Trading > Risk.")


def validate_update(updates: Mapping, current: Mapping) -> None:
    """Refuse a per-trade sizing save that would leave trades unplaceable.

    Checks only what this save touches: an old bad value elsewhere on the row
    must not block an unrelated setting (this install holds max_lot_size = 0
    as this is written).
    """
    if not {"strategy_lot_size", "max_lot_size"} & set(updates):
        return
    merged = {**current, **updates}

    if "max_lot_size" in updates and max_lot(merged) < MIN_LOT:
        raise ValueError(
            f"Maximum lot size must be at least {MIN_LOT}. At 0 every trade sizes to 0 lots.")

    fixed = _num(merged, "strategy_lot_size")
    if 0 < fixed < MIN_LOT:
        raise ValueError(f"A fixed lot size must be at least {MIN_LOT}.")
    if fixed > max_lot(merged):
        raise ValueError(
            f"Fixed lot size {fixed:g} is above Maximum lot size {max_lot(merged):g}. "
            "Raise the maximum first, or lower the lot size.")
