"""The panel's shared vocabulary: its Screen result type, the callback-data
helpers, and the channel lookups every section needs.

Imported BY the section modules, so nothing here may import them back.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Optional

from backend.src.db import database as db_module
from backend.src.services.broker import ea_templates
from backend.src.services.positions import panel_repo
from backend.src.utils.models import STRATEGY_NAMES

log = logging.getLogger(__name__)


CB = "p"          # callback-data namespace

SEP = "|"

# The panel's own pseudo-channel for orders placed by hand rather than from
# any signal source. Matches open_manual_market_order's default source tag so
# manual trades opened here land in the bucket the rest of the app already
# reports them under.
MANUAL = "Manual"

MANUAL_SOURCE = "manual_market"

class Screen:
    """What engine.py should do with the result of a panel interaction.

    mode:
      'edit'         -- replace the panel message in place (normal navigation)
      'send'         -- post a new message, leaving the panel intact
      'force_reply'  -- post a reply-prompt for a typed value
      'delete'       -- remove the panel message
      'noop'         -- nothing but the toast
    """

    __slots__ = ("text", "keyboard", "toast", "mode")

    def __init__(self, text: str = "", keyboard: Optional[list] = None,
                 toast: str = "", mode: str = "edit"):
        self.text = text
        self.keyboard = keyboard
        self.toast = toast
        self.mode = mode

def _cb(*parts) -> str:
    """Build callback data. Telegram hard-caps this at 64 bytes and silently
    rejects the whole keyboard if any button exceeds it, so assert rather
    than ship a panel whose buttons do nothing."""
    data = SEP.join([CB] + [str(p) for p in parts])
    if len(data.encode()) > 64:
        raise ValueError(f"callback data too long ({len(data.encode())}B): {data}")
    return data

def _btn(text: str, *parts) -> dict:
    return {"text": text, "callback_data": _cb(*parts)}

def _slug(name: str) -> str:
    return hashlib.md5((name or "").encode()).hexdigest()[:8]

def _channel_rec(name: str, strategy: Optional[str]) -> dict:
    template = None
    if ea_templates.is_template_override(strategy):
        template = ea_templates.template_name_from_override(strategy)
    paused = False
    try:
        _, paused = db_module.get_channel_lot_mult(name)
    except Exception:
        pass
    return {
        "name":     name,
        "slug":     _slug(name),
        "strategy": strategy,
        "template": template,
        "paused":   bool(paused),
    }

def channel_list() -> list[dict]:
    """Every channel the panel can act on, in the same order as the app's own
    Channel Strategy tab, with Manual appended."""
    recs: list[dict] = []
    try:
        for name, info in db_module.get_all_channel_strategy_overrides().items():
            recs.append(_channel_rec(name, info.get("strategy")))
    except Exception as e:
        log.warning("[Panel] channel list failed: %s", e)
    try:
        rs = db_module.get_risk_settings()
        manual_strategy = rs.get("trade_strategy")
    except Exception:
        manual_strategy = None
    recs.append(_channel_rec(MANUAL, manual_strategy))
    return recs

def _channel(slug: str) -> Optional[dict]:
    return next((c for c in channel_list() if c["slug"] == slug), None)

def _short(name: str, limit: int = 18) -> str:
    """Channel names run long ('GOLD DIGGERS INSTITUTIONAL'); Telegram button
    labels get clipped mid-word by the client, so clip deliberately instead."""
    name = name or "?"
    return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"

def _strategy_label(strategy: Optional[str]) -> str:
    if not strategy:
        return "inherit global"
    if strategy == "auto":
        return "Auto (AI)"
    if ea_templates.is_template_override(strategy):
        return f"Template: {ea_templates.template_name_from_override(strategy)}"
    return STRATEGY_NAMES.get(strategy, strategy)

def _channel_open_trades(chan: dict) -> list[dict]:
    """Open trades belonging to this channel.

    Matches the channel name and the 'Telegram Auto (<name>)' variant the
    auto-execution path writes, the same pair get_channel_trust checks -- a
    channel's trades are split across both spellings, and closing only one
    set would leave live positions behind while reporting 'all closed'."""
    name = chan["name"]
    variants = [name, f"Telegram Auto ({name})"]
    if name == MANUAL:
        variants = [MANUAL_SOURCE, "Manual Signal"]
    marks = ",".join("?" for _ in variants)
    return panel_repo.open_trades_for_sources(variants)

def _trade_push_sl_pips(t: dict) -> float:
    """manual_sl_push_pips for this trade's template, or 0 if it isn't a
    template trade, the template has bot commands off (tg_cmd_enabled), or
    no push amount is configured -- any of which hides the Push SL button
    rather than showing one that would just refuse when tapped."""
    strategy = t.get("strategy") or ""
    if not ea_templates.is_template_override(strategy):
        return 0.0
    tpl = ea_templates.get_ea_template(ea_templates.template_name_from_override(strategy))
    if not tpl or not tpl.get("tg_cmd_enabled"):
        return 0.0
    return float(tpl.get("manual_sl_push_pips") or 0)

def _dot(flag) -> str:
    return "\U0001f7e2" if flag else "\U0001f534"

def _money(value) -> str:
    return f"${float(value or 0):g}"
