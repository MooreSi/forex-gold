"""Learned parser rules and the AI-recovered review queue, server side.

Moved verbatim out of `server.py` -- same methods, same bodies, same names --
to keep that file inside its size budget. `SyncServer` mixes this in.

The mirror image of `sync/_peer_data.py` on the client. Both nodes run their
own Telethon session and parse every message independently, so an approval
made on one is worth nothing to the other unless it is copied across: without
it the second node pays for its own AI fallback and asks for the same approval
again on the same message shape.

Inbound payloads come from the peer, so each handler guards on what it cannot
do without rather than writing a half-row.
"""
from __future__ import annotations

from typing import Optional

import json
import logging

from backend.src.db import database as db_module
from backend.src.services.cluster.sync.protocol import (
    MSG_AI_RECOVERED_PUSH, MSG_LEARNED_RULE_SYNC, MSG_AI_RECOVERED_SIGNAL_SYNC,
    make,
)

log = logging.getLogger(__name__)


class ServerPeerDataMixin:
    def _handle_learned_rule_sync(self, msg: dict) -> None:
        """A parser rule approved on the Mac — mirror it here so the VPS's
        own independent Telethon session also parses future messages of this
        shape deterministically instead of needing its own AI fallback."""
        channel_name = msg.get("channel_name")
        pattern = msg.get("pattern")
        if not channel_name or not pattern:
            return
        db_module.save_synced_learned_rule(
            channel_name, msg.get("rule_type", "ai_derived_parser"), pattern,
            msg.get("action", "auto_parse"), msg.get("notes", ""),
            msg.get("source_msg_id"),
        )
        log.info("[SyncServer] mirrored learned rule from Mac: %s", channel_name)

    # ── The trading clock ────────────────────────────────────────────────
    #
    # The user's own local time is the machine's own clock -- on the machine
    # where the user is. This is the VPS, and it is not. An offset can be
    # configured for exactly that, but a fixed number does not follow daylight
    # saving: +60 for British Summer Time stays +60 in November, an hour wrong
    # for five months with nothing reporting it.
    #
    # So the Mac reports its own current offset and this adopts it. When the
    # Mac's clocks change, its next message carries the new value.
    #
    # One-way on purpose. The machine where the user actually is is the
    # authority on what time it is there, so the server adopts from the client
    # and never the reverse. `trading_clock_offset_min` is deliberately absent
    # from _SYNCED_SETTINGS_KEYS for the same reason -- as an ordinary synced
    # setting the broadcast would push this value back to the Mac and the Mac
    # would start running on its server's clock.

    def _apply_peer_clock_offset(self, msg: dict) -> None:
        """Adopt the peer's UTC offset as this node's trading clock."""
        from backend.src.utils.trading_clock import (
            SETTING_KEY, configured_offset_minutes,
        )

        raw = msg.get("clock_offset_min")
        if raw is None:
            return
        offset = configured_offset_minutes({SETTING_KEY: raw})
        if offset is None:
            log.warning("[SyncServer] ignoring unusable clock offset from the "
                        "Mac: %r", raw)
            return
        try:
            current = db_module.get_risk_settings().get(SETTING_KEY)
            if current is not None and int(current) == offset:
                return          # runs on every ping; only write on a change
            db_module.update_risk_settings({SETTING_KEY: offset},
                                           _from_sync=True)
        except Exception as e:
            # Runs inside the connection handler and on every ping. Dropping
            # the link over a clock detail would be worse than the wrong hour.
            log.warning("[SyncServer] could not apply the Mac's clock offset "
                        "(%s) — keeping this machine's own clock", e)
            return
        log.info("[SyncServer] trading clock now follows the Mac: UTC%+d:%02d",
                 offset // 60, abs(offset) % 60)

    # ── AI-recovered signal review queue (Telegram > Reader Logic > AI tab) ──

    def _handle_ai_recovered_signal_sync(self, msg: dict) -> None:
        """Mirror a create/approve/rule_result/discard event from the Mac so
        this node's own copy of the review queue (and its UI) stays
        identical — see sync/protocol.py's MSG_AI_RECOVERED_SIGNAL_SYNC
        comment for why the queue itself, not just the resulting rule,
        needs to be shared."""
        action = msg.get("action")
        tg_message_id = msg.get("tg_message_id")
        if not action or not tg_message_id:
            return
        if action == "created":
            if msg.get("message_type") == "sl_adjustment":
                db_module.save_ai_recovered_sl_adjustment(
                    tg_message_id, msg.get("channel_name", ""), msg.get("raw_text", ""),
                    msg.get("new_stop_loss"), msg.get("confidence", 0.0), msg.get("reasoning", ""),
                )
            else:
                parsed = {k: msg.get(k) for k in
                          ("direction", "entry_low", "entry_high", "stop_loss",
                           "tp1", "tp2", "tp3", "tp4", "tp5", "tp6", "tp7", "tp8")}
                db_module.save_ai_recovered_signal(
                    tg_message_id, msg.get("channel_name", ""), msg.get("raw_text", ""),
                    parsed, msg.get("confidence", 0.0), msg.get("reasoning", ""),
                )
        elif action == "approved":
            db_module.mark_ai_recovered_signal_approved_by_tg_id(tg_message_id)
        elif action == "rule_result":
            db_module.mark_ai_recovered_signal_rule_result_by_tg_id(
                tg_message_id, bool(msg.get("rule_generated")), msg.get("rule_gen_note", ""),
            )
        elif action == "discarded":
            db_module.discard_ai_recovered_signal_by_tg_id(tg_message_id)
        log.info("[SyncServer] mirrored ai_recovered_signal %s from Mac: %s", action, tg_message_id)

    async def _handle_ai_recovered_pull(self, ws) -> None:
        rows = db_module.get_unresolved_ai_recovered_signals()
        await ws.send(json.dumps(make(MSG_AI_RECOVERED_PUSH, signals=rows)))

    async def push_own_ai_recovered_signal(self, action: str, **fields) -> None:
        """Call right after the local mutator (save_ai_recovered_signal,
        mark_ai_recovered_signal_approved, mark_ai_recovered_signal_rule_result,
        discard_ai_recovered_signal) so the Mac's mirrored copy of this row
        stays in lockstep — best-effort, same as push_own_trade_closed(); a
        missed delivery is caught by the periodic full-queue pull instead."""
        await self._broadcast(make(MSG_AI_RECOVERED_SIGNAL_SYNC, action=action, **fields))

    # AI provider config lives in config.yaml (backend.src.config), a
    # completely separate store from vantage_risk_settings — allowlisted
    # here rather than added to _SYNCED_SETTINGS_KEYS above, and includes the
    # DeepSeek key (an explicit user opt-in 2026-07-08) unlike that
    # credential-free mechanism. anthropic_api_key deliberately excluded —
    # the VPS may have its own separately-provisioned Claude key/account and
    # this sync was only asked for to fix DeepSeek not reaching the VPS.
    _AI_CONFIG_SYNC_KEYS = ("ai_provider", "claude_model", "deepseek_model", "deepseek_api_key")

    def _handle_ai_config_sync(self, msg: dict) -> None:
        updates = {k: v for k, v in (msg.get("updates") or {}).items()
                   if k in self._AI_CONFIG_SYNC_KEYS}
        if not updates:
            return
        import backend.src.config as cfg_module
        cfg_module.save_to_yaml(updates)  # also reloads config.py's own module-level cache
        # Mirrors what Settings > AI's save handlers do locally (engine._cfg[...] = ...)
        # — most AI call sites read config.py fresh each call (already covered by the
        # save_to_yaml() reload above), but a few (e.g. the Telegram AI fallback) use
        # this engine's own cached _cfg dict, which save_to_yaml() has no way to reach.
        if self._main_engine is not None:
            for k, v in updates.items():
                self._main_engine._cfg[k] = v
        log.info("[SyncServer] applied AI config from Mac: %s",
                 {k: ("***" if k.endswith("_key") else v) for k, v in updates.items()})

    async def push_own_learned_rule(
        self, channel_name: str, rule_type: str, pattern: str, action: str,
        notes: str, source_msg_id: Optional[str],
    ) -> None:
        """Call after this node saves a learned rule locally, to forward it
        live to the Mac if connected — best-effort, same as
        push_own_trade_closed(); a missed delivery just means the Mac keeps
        needing its own AI fallback for this message shape until it
        independently gets and approves the same extraction."""
        await self._broadcast(make(
            MSG_LEARNED_RULE_SYNC, channel_name=channel_name, rule_type=rule_type,
            pattern=pattern, action=action, notes=notes, source_msg_id=source_msg_id,
        ))

    # ── Model snapshot (manual, on-demand) ───────────────────────────────────
