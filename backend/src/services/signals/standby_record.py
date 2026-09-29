"""The standby node's Telegram scan: record what each message says, do nothing else.

Owner, 2026-09-29: the node that is not trading keeps "parsing for display
only, no trades, no alerts". docs/todo/010-telegram-parses-on-the-active-node.md.

Both nodes of a pair run their own Telethon session on the same account and
see every message. Until this module the standby node ran the whole pipeline:
it queued signals, tried to open them (refused by open_trade's stand-down
gate, one error per signal), sent "signal detected" alerts, and could act on
a CLOSE ALL or RISK FREE message against the MT5 account both nodes share.

This is a separate path rather than flags threaded through scan_messages,
because every step of that pipeline can act: the IME and limit-order paths
place orders, the keyword triggers close and move stops, the SL-adjustment
path modifies trades, the edit handler can flatten a position, and
classify_and_parse itself sends a currency alert and asks the paid AI. A
flag missed on one of those is how the standby node would trade. Here there
is nothing to miss: the parsers below are pure, and the one write is the
display row.

The row is also what stops a backlog firing on a hand-over: a message the
standby node recorded already has a row when that node becomes the trader,
so scan_messages' dedup probe treats it as seen instead of new.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from backend.src.db import database as db_module
from backend.src.services.signals import tg_repo
from backend.src.services.signals.parser import (
    SIGNAL_PREFIX, _CURRENCY_RE, is_format_ab_signal, is_gd2_message,
    parse_format_ab_partial, parse_gd2_instant_entry, parse_gd2_partial,
    parse_gd2_signal, parse_gold_signal, parse_instant_entry,
    parse_limit_order_signal, parse_with_learned_rules,
)
from backend.src.services.telegram.keyword_triggers import parse_lexicon_direction_trigger

log = logging.getLogger(__name__)

# The row's status. Nothing selects it: the pending watcher, the edit handler's
# follow-up branch and the IME follow-up matcher all look for other statuses,
# so a standby row can never be activated.
STATUS_STANDBY = "standby"


def parse_for_display(text: str, channel_name: str, sig_prefix: str) -> Optional[dict]:
    """What this message would be read as, without acting on it. None for chatter."""
    cm = _CURRENCY_RE.search(text)
    if cm and cm.group(1).upper().replace("/", "").replace("-", "") != "XAUUSD":
        return None
    parsed = (parse_with_learned_rules(text, channel_name)
              or parse_limit_order_signal(text)
              or (parse_gold_signal(text) if is_format_ab_signal(text, sig_prefix) else None)
              or (parse_gd2_signal(text) if is_gd2_message(text) else None)
              or parse_gd2_partial(text) or parse_format_ab_partial(text))
    if parsed:
        return parsed
    trigger = (parse_instant_entry(text) or parse_gd2_instant_entry(text)
               or parse_lexicon_direction_trigger(text))
    if trigger:
        direction, price = trigger
        return {"direction": direction, "entry_low": price, "entry_high": price}
    return None


def record(reader: Any, msgs: list[dict], slot_groups: dict, rs: dict) -> list[dict]:
    """Record each new signal-shaped message once. Returns the new rows."""
    exclude_high_risk = bool(rs.get("exclude_high_risk", 0))
    recorded: list[dict] = []
    for msg in msgs:
        try:
            tg_id = str(msg.get("id") or "")
            group_id = str(msg.get("group_id") or "")
            text = (msg.get("text") or "").strip()
            if not tg_id or not text:
                continue
            if slot_groups and group_id and group_id not in slot_groups:
                continue
            if exclude_high_risk and "high risk" in text.lower():
                continue
            slot = slot_groups.get(group_id, 1)
            channel_name = reader.get_group_name(group_id) or f"Channel {slot}"
            # Read, never bootstrapped: creating a channel's config is the
            # trading node's job, and the Mac's copy is what the VPS mirrors.
            ch_cfg = db_module.get_channel_parser_config(channel_name) or {}
            if ch_cfg.get("parser_format") == "none" or not bool(ch_cfg.get("enabled", 1)):
                continue
            if tg_repo.get_tg_signal_meta(tg_id):
                continue
            levels = parse_for_display(text, channel_name,
                                       ch_cfg.get("signal_prefix") or SIGNAL_PREFIX)
            if not levels:
                continue
            if tg_repo.insert_tg_signal_if_new(
                    tg_id, group_id, channel_name, msg.get("sender_name", ""),
                    msg.get("timestamp") or "", text, levels, STATUS_STANDBY):
                log.info("[%s] Standby node: tg_id=%s %s recorded for display only; "
                         "the active node trades it", channel_name, tg_id,
                         levels.get("direction"))
                recorded.append(levels | {"tg_message_id": tg_id, "auto_executed": False,
                                          "source_label": channel_name})
        except Exception as e:
            log.warning("[standby] could not record tg_id=%s: %s", msg.get("id"), e)
    return recorded
