"""Telegram channel set-up over the node link: the Mac's reaches the VPS (owner, 2026-09-29).

The VPS reads Telegram itself when it trades; the Mac is where the channel
set-up is edited. docs/todo/010-telegram-parses-on-the-active-node.md. Three
things travel, none of which did before:

- **slots**: which Telegram channel each reader slot listens to. On
  2026-09-29 the Mac had three and the VPS two; Gold Diggers Scalping was
  Mac-only, so none of its signals could trade from the VPS.
- **parser config** (`channel_parser_config`): per channel, whether it is
  scanned at all, its signal prefix and its IME flag.
- **Logic Keywords lexicons**: the phrase lists behind CLOSE ALL, RISK FREE
  and the BUY/SELL order boxes, which close trades and fire market orders.

Two more joined on 2026-09-29, found auditing what else stayed per node
after ticket 2108608418 traded from a channel switched off on the Mac:

- **channel pauses set by hand** (`channel_performance.paused` with
  `manual_override = 1`). A pause the scorecard set is not sent: each node's
  scorecard pauses from its own results.
- **the news blackout** (Trading > News), which lives in config.yaml rather
  than the risk row, so the settings sync never carried it.

A Mac that does not send a part (an older one) leaves that part alone, and
the answer names only the parts that were sent.

Same shape as the EA templates (_ea_templates_sync.py): the Mac sends the
whole set on connect, within `_CHECK_EVERY_S` of any change and every
`_RESEND_EVERY_S` regardless, noticed by digest rather than by hooking each
writer. The VPS writes only what differs and answers with what it did.

Two things the VPS never does. It never removes a channel config or clears a
slot the Mac did not send: a Mac that has never logged in to Telegram sends
no slots, and matching that would stop every Telegram trade. And it never
receives a Telethon session or credential: only channel ids and names.

A payload with one bad part writes nothing: this arrives off the network and
its values become SQL parameters and Telegram entity ids.

Pinned by tests/core/test_channel_setup_sync.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

from backend.src.db import database as db_module
from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, MSG_CHANNEL_SETUP, MSG_CHANNEL_SETUP_ACK, make,
)
from backend.src.services.telegram import keywords

log = logging.getLogger(__name__)

_CHECK_EVERY_S = 5.0
_RESEND_EVERY_S = 600.0
_NUM_SLOTS = 3  # reader_common._NUM_SLOTS; a slot outside it is refused
_TEXT_FIELDS = ("parser_format", "signal_prefix", "notes")
_FLAG_FIELDS = ("instant_entry_enabled", "enabled")
# news_calendar.get_blackout_settings() names -> config.yaml keys. Its clamp
# (0..240 minutes) and impact sets are the validation, so nothing the VPS
# writes is a value its own calendar would have to correct.
_BLACKOUT_KEYS = {"enabled": "news_blackout_enabled", "impact": "news_blackout_impact",
                  "minutes_before": "news_blackout_minutes_before",
                  "minutes_after": "news_blackout_minutes_after"}
_BLACKOUT_MAX_MIN = 240


def _parser_configs() -> list[dict]:
    return [{"channel_name": r["channel_name"],
             **{k: r[k] for k in _TEXT_FIELDS}, **{k: int(r[k]) for k in _FLAG_FIELDS}}
            for r in db_module.get_all_channel_parser_configs()]


def _saved_slots() -> list[dict]:
    from backend.src.services.telegram import repo as telegram_repo
    row = telegram_repo.get_selected_groups_json()
    try:
        saved = json.loads(row[0]) if row else []
    except (TypeError, ValueError):
        return []
    return [{"slot": int(g["slot"]), "group_id": int(g["group_id"]),
             "group_name": str(g.get("group_name") or "")}
            for g in saved if isinstance(g, dict) and g.get("group_id")]


def _manual_pauses() -> list[dict]:
    perf = db_module.get_channel_performance_map()
    return [{"source": src, "paused": int(row["paused"])}
            for src, row in sorted(perf.items()) if row["manual_override"]]


def _news_blackout() -> dict:
    from backend.src.utils import news_calendar
    s = news_calendar.get_blackout_settings()
    return {k: s[k] for k in _BLACKOUT_KEYS}


def snapshot() -> dict:
    """The Mac's side: everything that travels, in a stable order."""
    return {"parser_configs": _parser_configs(),
            "lexicons": keywords.get_all_lexicons(),
            "slots": _saved_slots(),
            "channel_pauses": _manual_pauses(),
            "news_blackout": _news_blackout()}


def _digest(snap: dict) -> str:
    return hashlib.sha256(json.dumps(snap, sort_keys=True).encode()).hexdigest()


def _config_problem(configs) -> str:
    if not isinstance(configs, list):
        return "parser_configs is not a list"
    for c in configs:
        if not isinstance(c, dict) or not isinstance(c.get("channel_name"), str) \
                or not c["channel_name"].strip():
            return "a parser config has no channel name"
        if not all(isinstance(c.get(k), str) for k in _TEXT_FIELDS):
            return f"parser config for {c['channel_name']!r} has a non-text field"
        if not all(c.get(k) in (0, 1) and not isinstance(c.get(k), float) for k in _FLAG_FIELDS):
            return f"parser config for {c['channel_name']!r} has a flag that is not 0 or 1"
    return ""


def _lexicon_problem(lexicons) -> str:
    if not isinstance(lexicons, dict):
        return "lexicons is not a mapping"
    for cat, phrases in lexicons.items():
        if cat not in keywords.DEFAULT_LEXICONS:
            return f"unknown Logic Keywords category {cat!r}"
        if not isinstance(phrases, list) or not all(isinstance(p, str) for p in phrases):
            return f"lexicon {cat!r} is not a list of phrases"
    return ""


def _is_flag(v) -> bool:
    return v in (0, 1) and not isinstance(v, float)


def _pauses_problem(pauses) -> str:
    if pauses is None:
        return ""
    if not isinstance(pauses, list):
        return "channel_pauses is not a list"
    for p in pauses:
        if not isinstance(p, dict) or not isinstance(p.get("source"), str) \
                or not p["source"].strip():
            return "a channel pause has no channel name"
        if not _is_flag(p.get("paused")):
            return f"channel pause for {p['source']!r} is not 0 or 1"
    return ""


def _blackout_problem(blackout) -> str:
    if blackout is None:
        return ""
    from backend.src.utils import news_calendar
    if not isinstance(blackout, dict) or set(blackout) != set(_BLACKOUT_KEYS):
        return "news_blackout does not carry exactly its four settings"
    if not isinstance(blackout["enabled"], bool):
        return "news_blackout enabled is not true or false"
    if blackout["impact"] not in news_calendar._IMPACT_SETS:
        return f"unknown news blackout impact {blackout['impact']!r}"
    for k in ("minutes_before", "minutes_after"):
        v = blackout[k]
        if not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= _BLACKOUT_MAX_MIN:
            return f"news blackout {k} is not a whole number from 0 to {_BLACKOUT_MAX_MIN}"
    return ""


def _apply_pauses(pauses: list) -> list[str]:
    mine = db_module.get_channel_performance_map()
    written = []
    for p in pauses:
        row = mine.get(p["source"])
        if row and row["manual_override"] and int(row["paused"]) == p["paused"]:
            continue
        db_module.set_channel_paused(p["source"], bool(p["paused"]))
        written.append(p["source"])
    return written


def _apply_blackout(blackout: dict) -> bool:
    if _news_blackout() == blackout:
        return False
    import backend.src.config as cfg_module
    cfg_module.save_to_yaml({_BLACKOUT_KEYS[k]: blackout[k] for k in _BLACKOUT_KEYS})
    return True


def apply_settings(payload: dict) -> dict:
    """The VPS's side for the database and config half: parser configs,
    lexicons, channel pauses and the news blackout."""
    configs, lexicons = payload.get("parser_configs"), payload.get("lexicons", {})
    pauses, blackout = payload.get("channel_pauses"), payload.get("news_blackout")
    problem = (_config_problem(configs) or _lexicon_problem(lexicons)
               or _pauses_problem(pauses) or _blackout_problem(blackout))
    if problem:
        return {"parser_configs": [], "lexicons": [], "error": problem}
    mine = {c["channel_name"]: c for c in _parser_configs()}
    written_cfg = []
    for c in configs:
        if mine.get(c["channel_name"]) == {k: c[k] for k in ("channel_name", *_TEXT_FIELDS, *_FLAG_FIELDS)}:
            continue
        db_module.save_channel_parser_config(
            c["channel_name"], c["parser_format"], c["signal_prefix"],
            bool(c["instant_entry_enabled"]), bool(c["enabled"]), c["notes"])
        written_cfg.append(c["channel_name"])
    written_lex = []
    for cat, phrases in lexicons.items():
        if keywords.get_lexicon(cat) != [p.strip().upper() for p in phrases if p.strip()]:
            keywords.set_lexicon(cat, phrases)
            written_lex.append(cat)
    out = {"parser_configs": written_cfg, "lexicons": written_lex, "error": ""}
    if pauses is not None:
        out["channel_pauses"] = _apply_pauses(pauses)
    if blackout is not None:
        out["news_blackout"] = _apply_blackout(blackout)
    return out


def _slots_problem(slots) -> str:
    if not isinstance(slots, list):
        return "slots is not a list"
    for g in slots:
        if not isinstance(g, dict) or not isinstance(g.get("slot"), int) \
                or not 1 <= g["slot"] <= _NUM_SLOTS:
            return "a slot number is out of range"
        if not isinstance(g.get("group_id"), int) or isinstance(g.get("group_id"), bool):
            return f"slot {g['slot']} has no channel id"
        if not isinstance(g.get("group_name"), str) or not g["group_name"].strip():
            return f"slot {g['slot']} has no channel name"
    return ""


async def apply_slots(reader, slots) -> dict:
    """The VPS's side for the reader: select and start what differs."""
    problem = _slots_problem(slots)
    if problem:
        return {"changed": [], "errors": [problem]}
    if not slots:
        return {"changed": [], "errors": []}
    if reader is None:
        return {"changed": [], "errors": ["this node has no Telegram reader"]}
    have = {s["slot"]: s for s in reader.get_status().get("slots", [])}
    changed, errors, touched = [], [], False
    for g in slots:
        mine = have.get(g["slot"]) or {}
        same = mine.get("group_id") is not None and int(mine["group_id"]) == g["group_id"]
        if same and mine.get("listener_active"):
            continue
        if not same:
            await reader.select_group(g["group_id"], g["group_name"], g["slot"])
            changed.append(g["slot"])
        touched = True
        started = await reader.start_listener(g["slot"])
        if (started or {}).get("error"):
            errors.append(f"slot {g['slot']} ({g['group_name']}): {started['error']}")
    if touched:
        reader.save_group_selections()
    return {"changed": changed, "errors": errors}


def last_result() -> dict:
    """The VPS's last answer to this Mac, {} before the first (the dashboard's read)."""
    from backend.src.services.cluster.sync import client
    return dict(getattr(client.get_instance(), "channel_setup_result", None) or {})


class ClientChannelSetupMixin:
    async def _push_channel_setup_if_changed(self) -> bool:
        """Send the set-up if it changed, or is due a resend. True if sent."""
        if getattr(self, "_ws", None) is None or self.conn_state != CONN_CONNECTED:
            return False
        snap = await db_module.to_db_thread(snapshot)
        digest = _digest(snap)
        now = time.monotonic()
        last_digest, last_at = self.__dict__.get("_channel_setup_sent", (None, 0.0))
        if digest == last_digest and now - last_at < _RESEND_EVERY_S:
            return False
        await self._ws.send(json.dumps(make(MSG_CHANNEL_SETUP, **snap)))
        self._channel_setup_sent = (digest, now)
        return True

    async def _channel_setup_sync_loop(self) -> None:
        self._channel_setup_sent = (None, 0.0)  # a new connection always sends
        while self.conn_state == CONN_CONNECTED:
            try:
                await self._push_channel_setup_if_changed()
            except Exception as e:
                log.warning("[SyncClient] channel set-up send failed (resent on reconnect): %s", e)
                break
            await asyncio.sleep(_CHECK_EVERY_S)

    def _on_channel_setup_ack(self, msg: dict) -> None:
        self.channel_setup_result = msg
        if msg.get("errors"):
            log.warning("[SyncClient] the VPS could not take all of the channel set-up: %s",
                        "; ".join(msg["errors"]))
        elif msg.get("parser_configs") or msg.get("lexicons") or msg.get("slots") \
                or msg.get("channel_pauses") or msg.get("news_blackout"):
            log.info("[SyncClient] channel set-up on the VPS: configs %s, keywords %s, slots %s, "
                     "pauses %s, news blackout %s",
                     msg.get("parser_configs"), msg.get("lexicons"), msg.get("slots"),
                     msg.get("channel_pauses"), msg.get("news_blackout"))


class ServerChannelSetupMixin:
    async def _handle_channel_setup(self, ws, msg: dict) -> None:
        errors = []
        try:
            settings = await db_module.to_db_thread(apply_settings, msg)
        except Exception as e:
            settings = {"parser_configs": [], "lexicons": [], "error": str(e)}
        if settings["error"]:
            errors.append(settings["error"])
            slots = {"changed": [], "errors": []}
        else:
            reader = getattr(getattr(self, "_main_engine", None), "_tg_reader", None)
            try:
                slots = await apply_slots(reader, msg.get("slots", []))
            except Exception as e:
                slots = {"changed": [], "errors": [f"reader: {e}"]}
            errors += slots["errors"]
        # Only the parts the Mac sent are answered (an older Mac sends none).
        extra = {k: settings[k] for k in ("channel_pauses", "news_blackout") if k in settings}
        if errors:
            log.error("[SyncServer] channel set-up from the Mac: %s", "; ".join(errors))
        elif settings["parser_configs"] or settings["lexicons"] or slots["changed"] \
                or any(extra.values()):
            log.info("[SyncServer] channel set-up from the Mac: configs %s, keywords %s, "
                     "slots %s, pauses %s, news blackout %s",
                     settings["parser_configs"], settings["lexicons"], slots["changed"],
                     extra.get("channel_pauses"), extra.get("news_blackout"))
        await ws.send(json.dumps(make(
            MSG_CHANNEL_SETUP_ACK, parser_configs=settings["parser_configs"],
            lexicons=settings["lexicons"], slots=slots["changed"], errors=errors, **extra)))
