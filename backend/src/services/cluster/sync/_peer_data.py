"""Learned parser rules and the AI-recovered review queue, mirrored between
nodes.

Moved verbatim out of `client.py` -- same methods, same bodies, same names --
to keep that file inside its size budget. `SyncClient` mixes this in, so
`self._ws` and `self.conn_state` resolve exactly as they did inline.

One concern, in both directions. INBOUND, a rule approved on the VPS is copied
here so this node's independent Telethon session parses the same message shape
deterministically, instead of paying for its own AI fallback and asking for the
same approval again. OUTBOUND, this node pushes its own approvals the same way.

The payloads arrive from the peer, so every handler guards on the fields it
cannot do without and returns quietly rather than writing a half-row -- see
tests/core/test_sync_peer_data.py, which owns that behaviour.
"""
from __future__ import annotations

from typing import Optional

import json
import logging

from backend.src.db import database as db_module
from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, MSG_AI_CONFIG_SYNC, MSG_AI_RECOVERED_SIGNAL_SYNC,
    MSG_LEARNED_RULE_SYNC, make,
)

log = logging.getLogger(__name__)


class PeerDataMixin:
    def _handle_learned_rule_sync(self, msg: dict) -> None:
        """A parser rule approved on the VPS — mirror it into this node's own
        channel_learned_rules so this Mac's independent Telethon session also
        parses future messages of this shape deterministically, instead of
        needing its own separate AI fallback + approval for the same format."""
        channel_name = msg.get("channel_name")
        pattern = msg.get("pattern")
        if not channel_name or not pattern:
            return
        db_module.save_synced_learned_rule(
            channel_name, msg.get("rule_type", "ai_derived_parser"), pattern,
            msg.get("action", "auto_parse"), msg.get("notes", ""),
            msg.get("source_msg_id"),
        )
        log.info("[SyncClient] mirrored learned rule from VPS: %s", channel_name)

    # ── AI-recovered signal review queue (Telegram > Reader Logic > AI tab) ──

    def _handle_ai_recovered_signal_sync(self, msg: dict) -> None:
        """Mirror a create/approve/rule_result/discard event from the VPS —
        see sync/protocol.py's MSG_AI_RECOVERED_SIGNAL_SYNC comment."""
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
        log.info("[SyncClient] mirrored ai_recovered_signal %s from VPS: %s", action, tg_message_id)

    def _apply_ai_recovered_snapshot_row(self, row: dict) -> None:
        """Applies one row of a MSG_AI_RECOVERED_PUSH full-queue snapshot —
        same upsert-by-tg_message_id path as a live 'created' event, just
        driven from a periodic full resync instead of a single push (see
        _ai_recovered_pull_loop). This is what backfills anything created
        while this node was disconnected, or before this sync feature
        existed at all."""
        tg_message_id = row.get("tg_message_id")
        if not tg_message_id:
            return
        if row.get("message_type") == "sl_adjustment":
            db_module.save_ai_recovered_sl_adjustment(
                tg_message_id, row.get("channel_name", ""), row.get("raw_text", ""),
                row.get("new_stop_loss"), row.get("confidence", 0.0), row.get("reasoning", ""),
            )
        else:
            parsed = {k: row.get(k) for k in
                      ("direction", "entry_low", "entry_high", "stop_loss",
                       "tp1", "tp2", "tp3", "tp4", "tp5", "tp6", "tp7", "tp8")}
            db_module.save_ai_recovered_signal(
                tg_message_id, row.get("channel_name", ""), row.get("raw_text", ""),
                parsed, row.get("confidence", 0.0), row.get("reasoning", ""),
            )

    async def push_ai_recovered_signal(self, action: str, **fields) -> None:
        """Best-effort, fire-and-forget — same pattern as push_learned_rule().
        A missed delivery is caught by the periodic full-queue pull instead
        (_ai_recovered_pull_loop)."""
        if self._ws is not None and self.conn_state == CONN_CONNECTED:
            try:
                await self._ws.send(json.dumps(make(
                    MSG_AI_RECOVERED_SIGNAL_SYNC, action=action, **fields,
                )))
            except Exception as e:
                log.debug("[SyncClient] push_ai_recovered_signal failed: %s", e)

    async def push_learned_rule(
        self, channel_name: str, rule_type: str, pattern: str, action: str,
        notes: str, source_msg_id: Optional[str],
    ) -> None:
        """Best-effort, fire-and-forget — same pattern as push_trade_closed().
        A rule approved on this Mac was already saved to this node's own DB
        before this is called; a missed delivery here just means the VPS
        keeps using its AI fallback for this message shape until it
        independently gets (and someone approves) the same extraction."""
        if self._ws is not None and self.conn_state == CONN_CONNECTED:
            try:
                await self._ws.send(json.dumps(make(
                    MSG_LEARNED_RULE_SYNC, channel_name=channel_name, rule_type=rule_type,
                    pattern=pattern, action=action, notes=notes, source_msg_id=source_msg_id,
                )))
            except Exception as e:
                log.debug("[SyncClient] push_learned_rule failed: %s", e)

    async def push_ai_config(self, updates: dict) -> None:
        """Mirror an AI provider/model/key change to the VPS (see
        SimulationEngine/Settings > AI's save handlers). Best-effort,
        fire-and-forget like push_trade_closed() — a missed delivery just
        means the VPS keeps its own AI config until the next successful
        save-on-Mac while connected."""
        if self._ws is not None and self.conn_state == CONN_CONNECTED:
            try:
                await self._ws.send(json.dumps(make(MSG_AI_CONFIG_SYNC, updates=updates)))
            except Exception as e:
                log.debug("[SyncClient] push_ai_config failed: %s", e)
