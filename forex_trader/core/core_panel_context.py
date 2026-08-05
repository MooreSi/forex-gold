"""Channel roster and link status for the EA's on-chart panel (2026-08-05).

The panel's CH1/CH2/CH3 tabs are a SELECTOR, not a second config store:
picking a channel changes which saved EA template the panel is editing, and
nothing else. Channel name, channel id and the TELEGRAM / TG CMD lamps are
mirrors of what the app already holds -- the terminal never writes them.

That asymmetry is deliberate. Every editable control on the panel round-
trips through core_ea_templates (panel_action -> save -> set_template push),
so the app stays the single authority for anything that changes behaviour.
Channel identity is configured once in Settings > Telegram against a live
Telethon session; making it typeable into an MQL5 edit box would create a
second, unvalidated path to the one piece of config that decides which
signals get read at all.

Slot order matches TelegramReader's own slots, so CH1 here is the same CH1
the Telegram page shows.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

MAX_PANEL_CHANNELS = 3   # keep in step with telegram_reader._NUM_SLOTS


def _template_for_channel(name: str) -> str:
    """The EA template assigned to this channel, or "" when it inherits the
    global strategy instead."""
    from forex_trader.core import core_ea_templates as _et
    from forex_trader.core import database as db_module
    try:
        override = db_module.get_channel_strategy_override(name) or ""
    except Exception:
        return ""
    return _et.template_name_from_override(override) if _et.is_template_override(override) else ""


def build_context(tg_reader: Any, active_slot: int = 0) -> dict:
    """Assemble the panel_context payload.

    tg_reader may be None (the app runs perfectly well with Telegram off) --
    the channels then come from whatever has an EA template assigned, so the
    panel is still usable for a template-driven setup with no reader
    attached.
    """
    from forex_trader.core import core_ea_templates as _et
    from forex_trader.core import database as db_module

    channels: list[dict] = []
    tg_active = False
    tg_cmd = False

    if tg_reader is not None:
        try:
            st = tg_reader.get_status() or {}
            tg_active = str(st.get("auth_state") or "").lower() in ("connected", "authorized")
            for s in (st.get("slots") or [])[:MAX_PANEL_CHANNELS]:
                nm = s.get("group_name") or ""
                if not nm:
                    continue
                channels.append({
                    "name": nm,
                    "id": str(s.get("group_id") or ""),
                    "template": _template_for_channel(nm),
                    # A slot is live when either its push listener or its
                    # polling fallback is running -- the two are alternatives,
                    # not both-required, so an OR is the honest test.
                    "active": bool(s.get("listener_active") or s.get("poller_active")),
                })
        except Exception as e:
            log.debug("[Panel] telegram status unavailable: %s", e)

    if not channels:
        # No reader, or no slot configured: fall back to channels that have a
        # template assigned, so the panel still has something to edit.
        try:
            overrides = db_module.get_all_channel_strategy_overrides() or {}
        except Exception:
            overrides = {}
        for nm, v in overrides.items():
            ov = v.get("strategy_override") or v.get("strategy") or ""
            if not _et.is_template_override(ov):
                continue
            channels.append({
                "name": nm, "id": "",
                "template": _et.template_name_from_override(ov),
                "active": False,
            })
            if len(channels) >= MAX_PANEL_CHANNELS:
                break

    # TG CMD is a per-template switch (Telegram bot commands accepted for
    # trades this template opened), so it reads off the selected channel's
    # template rather than off the reader.
    sel = channels[active_slot] if 0 <= active_slot < len(channels) else None
    if sel and sel["template"]:
        try:
            tpl = _et.get_ea_template(sel["template"]) or {}
            tg_cmd = bool(tpl.get("tg_cmd_enabled"))
        except Exception:
            pass

    msg: dict = {
        "type": "panel_context",
        "channel_count": len(channels),
        "active_slot": active_slot,
        "tg_active": 1 if tg_active else 0,
        "tg_cmd": 1 if tg_cmd else 0,
    }
    for i, c in enumerate(channels, start=1):
        msg[f"ch{i}_name"]     = c["name"]
        msg[f"ch{i}_id"]       = c["id"]
        msg[f"ch{i}_template"] = c["template"]
        msg[f"ch{i}_active"]   = 1 if c["active"] else 0
    return msg


def template_for_slot(tg_reader: Any, slot: int) -> Optional[str]:
    """Which template the panel should edit when CH<slot+1> is selected.

    None when that slot has no channel or the channel has no template --
    the panel then keeps whatever it was showing, which is better than
    silently switching to an unrelated template.
    """
    ctx = build_context(tg_reader, active_slot=slot)
    name = ctx.get(f"ch{slot + 1}_template") or ""
    return name or None
