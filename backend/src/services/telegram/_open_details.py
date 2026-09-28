"""Two lines of the trade-open alert: the template's TP ladder when the row
holds none, and how the lot was sized (2026-09-28).

Reported on ticket 2103198838: the message showed SL but no TP, and nothing
said the lot came from a risk percentage. Kept out of `alerts.py`, which is
near its size budget.

Display only. Both lines are best-effort: a failed read costs the line, never
the alert.
"""
from __future__ import annotations

import logging

from backend.src.db import database as db_module
from backend.src.services.broker import ea_templates
from backend.src.services.risk import lot_sizing

log = logging.getLogger(__name__)

_MAX_TP_LEVELS = 8


def _template_for(trade: dict) -> dict | None:
    strategy = trade.get("strategy") or ""
    if not ea_templates.is_template_override(strategy):
        return None
    return ea_templates.get_ea_template(ea_templates.template_name_from_override(strategy))


def template_tp_lines(trade: dict) -> str:
    """The template's ladder as distances, for a template row with no TP prices.

    Distances, not prices: the row holds no resolved levels, so any price
    printed here would be one this app worked out after the fact rather than
    one the EA was given. "" for a non-template trade.
    """
    try:
        template = _template_for(trade)
    except Exception as e:
        log.debug("[alerts] template read failed for TP lines: %s", e)
        return ""
    if template is None:
        return ""
    lines = []
    for n in range(1, _MAX_TP_LEVELS + 1):
        pips = float(template.get(f"tp{n}_pips") or 0)
        if pips <= 0:
            continue
        pct = float(template.get(f"tp{n}_pct") or 0)
        lines.append(f"TP{n}: +{pips:g} pips ({pct:g}%)" if pct > 0 else f"TP{n}: +{pips:g} pips")
    if not lines:
        return "TP: none set by the template"
    return "\n".join(lines + ["(TP ladder from the template, managed by the EA)"])


def risk_line(trade: dict) -> str:
    """"Risk: X% of balance" when the lot came from a risk percentage, else "".

    The basis only. The channel lot multiplier, the signal-age shrink and the
    contradiction policy scale some routes' lots and not others, so the Lot
    line is the figure that was traded.
    """
    try:
        basis = lot_sizing.sizing_basis(db_module.get_risk_settings(), _template_for(trade))
    except Exception as e:
        log.debug("[alerts] sizing basis unavailable: %s", e)
        return ""
    return f"Risk: {basis}" if basis else ""
