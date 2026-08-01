"""Group selection, listeners, pollers and the message pipeline -- split verbatim from reader.py (M2 file-size
pass). Methods unchanged; composed back into TelegramReader in reader.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

from backend.src.db import database as db_module
from backend.src.services.telegram import repo as telegram_repo
from backend.src.services.telegram.reader_common import (
    _TELETHON_AVAILABLE, _NUM_SLOTS, _media_type, _safe_phone_log,
    AUTH_DISCONNECTED, AUTH_AWAITING_CODE, AUTH_AWAITING_2FA,
    AUTH_CONNECTED, AUTH_RECONNECTING, AUTH_FAILED,
)

if _TELETHON_AVAILABLE:
    from backend.src.services.telegram.reader_common import (
        TelegramClient, events,
        ApiIdInvalidError, FloodWaitError, PhoneCodeExpiredError,
        PhoneCodeInvalidError, PhoneNumberInvalidError,
        PasswordHashInvalidError, SessionPasswordNeededError,
        Channel, User,
    )

log = logging.getLogger(__name__)


class _ListenerMixin:
    # ── Groups ────────────────────────────────────────────────────────────────

    async def get_groups(self) -> list[dict]:
        if not _TELETHON_AVAILABLE or self._auth_state not in (AUTH_CONNECTED, AUTH_RECONNECTING):
            return []
        if self._client and not self._client.is_connected():
            ok = await self._reconnect()
            if not ok:
                return []
        try:
            dialogs = await self._client.get_dialogs()
            result  = []
            for d in dialogs:
                entity = d.entity
                if isinstance(entity, User):
                    continue
                kind = "channel" if isinstance(entity, Channel) and entity.broadcast else \
                       "supergroup" if isinstance(entity, Channel) else "group"
                result.append({
                    "id":   entity.id,
                    "name": d.name or getattr(entity, "title", ""),
                    "type": kind,
                    "unread_count": d.unread_count,
                })
            return result
        except Exception as e:
            self._add_error(str(e))
            return []

    async def select_group(self, group_id: int, group_name: str, slot: int = 1) -> dict:
        slot = max(0, min(_NUM_SLOTS - 1, slot - 1))   # 1-indexed → 0-indexed
        if self._listener_active[slot]:
            self._stop_listener(slot)
        self._group_ids[slot]      = int(group_id)
        self._group_names[slot]    = group_name
        self._group_entities[slot] = None
        await self._resolve_entity(slot)
        db_module.save_telegram_reader_event("group", "selected",
                                              f"Slot {slot+1}: {group_name} ({group_id})")
        return {
            "slot":       slot + 1,
            "group_id":   self._group_ids[slot],
            "group_name": self._group_names[slot],
        }

    async def start_listener(self, slot: int = 1) -> dict:
        slot = max(0, min(_NUM_SLOTS - 1, slot - 1))
        if not _TELETHON_AVAILABLE or self._auth_state != AUTH_CONNECTED:
            return {"error": "Not connected"}
        if not self._group_ids[slot]:
            return {"error": f"No group selected for slot {slot+1}"}
        if self._listener_active[slot]:
            return {"listener_active": True, "slot": slot + 1}
        task = asyncio.create_task(self._listener_task(slot))
        self._listener_tasks[slot] = task
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        await asyncio.sleep(0.1)
        return {"listener_active": True, "slot": slot + 1,
                "message": f"Slot {slot+1}: listening on '{self._group_names[slot]}'"}

    def stop_listener(self, slot: int = 1) -> None:
        slot = max(0, min(_NUM_SLOTS - 1, slot - 1))
        self._stop_listener(slot)

    # ── Listener internals ────────────────────────────────────────────────────

    def _stop_listener(self, slot: int) -> None:
        task = self._listener_tasks[slot]
        if task and not task.done():
            task.cancel()
            self._listener_tasks[slot] = None
        self._listener_active[slot] = False
        db_module.save_telegram_reader_event("listener", "stopped", f"Slot {slot+1} stopped")

    def _stop_all_listeners(self) -> None:
        for s in range(_NUM_SLOTS):
            self._stop_listener(s)

    async def _resolve_entity(self, slot: int):
        if self._group_entities[slot] is not None:
            return self._group_entities[slot]
        if not self._client or not self._group_ids[slot]:
            return None
        try:
            dialogs = await self._client.get_dialogs()
            for d in dialogs:
                entity = d.entity
                if int(getattr(entity, "id", 0) or 0) == int(self._group_ids[slot]):
                    self._group_entities[slot] = entity
                    self._check_group_renamed(slot, d.name or getattr(entity, "title", ""))
                    return entity
            self._group_entities[slot] = await self._client.get_entity(self._group_ids[slot])
            self._check_group_renamed(slot, getattr(self._group_entities[slot], "title", ""))
            return self._group_entities[slot]
        except Exception as e:
            self._add_error(f"Could not resolve entity slot {slot+1}: {e}")
            return None

    def _check_group_renamed(self, slot: int, live_title: str) -> None:
        """The Telegram group's real title is the source of truth -- if it no
        longer matches what we stored at select_group() time, the channel was
        renamed on Telegram's side. Cascade the rename across every DB table
        keyed by that name string (channel_parser_config, channel_performance,
        channel_strategy_rec, trade history, pending orders) so the app's
        Channel Strategy tab keeps showing one continuous row under the new
        name instead of forking into a stale orphan and a blank new one.
        Runs every time the entity is freshly resolved (reconnect, restart,
        or first select) -- cheap no-op UPDATEs once names already match."""
        live_title = (live_title or "").strip()
        old_name   = self._group_names[slot]
        if not live_title or not old_name or live_title == old_name:
            return
        try:
            db_module.sync_channel_rename(old_name, live_title)
            db_module.save_telegram_reader_event(
                "group", "renamed",
                f"Slot {slot+1}: '{old_name}' -> '{live_title}' ({self._group_ids[slot]})",
            )
        except Exception as e:
            self._add_error(f"Channel rename sync failed slot {slot+1}: {e}")
            return
        self._group_names[slot] = live_title
        self.save_group_selections()

    @staticmethod
    def _extract_sender_fields(sender) -> tuple[Optional[int], str]:
        """Extract (sender_id, sender_name) from a Telethon sender entity."""
        if sender is None:
            return None, ""
        sender_id = getattr(sender, "id", None)
        name = ""
        if hasattr(sender, "first_name"):
            parts = [getattr(sender, "first_name", None) or "",
                     getattr(sender, "last_name",  None) or ""]
            name = " ".join(p for p in parts if p).strip()
            if not name and getattr(sender, "username", None):
                name = f"@{getattr(sender, 'username')}"
        elif hasattr(sender, "title"):
            name = getattr(sender, "title", "") or ""
        return sender_id, name

    def _build_msg_dict(self, msg_obj, slot: int,
                        sender_id=None, sender_name: str = "") -> dict:
        """Build the message dict from a Telethon message object."""
        msg_date = getattr(msg_obj, "date", None)
        ts_utc   = msg_date.astimezone(timezone.utc).isoformat() if msg_date else None
        text     = getattr(msg_obj, "text",     None) or ""
        raw_text = getattr(msg_obj, "raw_text", None) or text
        return {
            "id":                  getattr(msg_obj, "id", None),
            "group_id":            self._group_ids[slot],
            "group_name":          self._group_names[slot] or "",
            "sender_id":           sender_id,
            "sender_name":         sender_name,
            "timestamp":           ts_utc,
            "received_at":         datetime.now(timezone.utc).isoformat(),
            "text":                text,
            "raw_text":            raw_text,
            "has_media":           getattr(msg_obj, "media", None) is not None,
            "media_type":          _media_type(msg_obj),
            "reply_to_message_id": getattr(msg_obj, "reply_to_msg_id", None),
            "forwarded":           getattr(msg_obj, "fwd_from", None) is not None,
        }

    async def _record_message(self, msg_obj, slot: int, sender=None) -> dict:
        """Buffer a message immediately using pre-resolved sender (or empty strings).
        Caller is responsible for scheduling sender resolution + DB write if needed.
        """
        sender_id, sender_name = self._extract_sender_fields(sender)
        msg = self._build_msg_dict(msg_obj, slot, sender_id, sender_name)
        self._buffer_message(msg)
        return msg

    async def _resolve_and_store(self, msg_obj, msg: dict) -> None:
        """Background task: resolve sender name then persist to DB.
        Runs after the message is already buffered so it never blocks the hot path.
        """
        try:
            sender = await msg_obj.get_sender()
            sid, sname = self._extract_sender_fields(sender)
            if sname:
                msg["sender_name"] = sname
            if sid:
                msg["sender_id"] = sid
        except Exception:
            pass
        try:
            db_module.store_telegram_message(msg)
        except Exception as e:
            log.debug("store_telegram_message failed: %s", e)

    def _buffer_message(self, msg: dict) -> bool:
        msg_id   = msg.get("id")
        group_id = msg.get("group_id")
        duplicate = any(m.get("id") == msg_id and m.get("group_id") == group_id
                        for m in self._msg_buffer)
        self._msg_buffer = [
            msg,
            *[m for m in self._msg_buffer
              if not (m.get("id") == msg_id and m.get("group_id") == group_id)],
        ]
        self._msg_buffer.sort(
            key=lambda m: (m.get("timestamp") or m.get("received_at") or "", int(m.get("id") or 0)),
            reverse=True,
        )
        self._msg_buffer = self._msg_buffer[:self._MSG_BUFFER]
        if not duplicate:
            self._messages_session += 1
        self._last_message_at = msg.get("received_at")
        return not duplicate

    def _make_message_handler(self, slot: int):
        # Handler returns as fast as possible — only enqueue, no awaits.
        # All network calls (get_sender) and DB writes happen in _event_processor.
        async def _handle_new_message(event):
            from backend.src.utils import latency_trace as _lt
            _lt.mark(getattr(event.message, "id", None), "t1_arrived")
            try:
                self._event_queue.put_nowait(("new", slot, event))
                _lt.mark(getattr(event.message, "id", None), "t2_queued")
            except asyncio.QueueFull:
                self._add_error(f"Event queue full — dropped message slot {slot+1}")
        return _handle_new_message

    def _make_edit_handler(self, slot: int):
        async def _handle_edit(event):
            try:
                self._event_queue.put_nowait(("edit", slot, event))
            except asyncio.QueueFull:
                self._add_error(f"Event queue full — dropped edit slot {slot+1}")
        return _handle_edit

    async def _event_processor(self) -> None:
        """Drains the event queue with minimal blocking.

        get_sender() (a potential Telegram API call) and the DB write are moved
        to a background task so the message is buffered and the scan-loop event
        is set before any network round-trips occur.
        """
        from backend.src.utils import latency_trace as _lt
        while True:
            try:
                kind, slot, event = await self._event_queue.get()
                # Gap from t1_arrived to here is the asyncio scheduling delay —
                # how long the message sat in the queue before this coroutine
                # was actually scheduled to run.
                _lt.mark(getattr(event.message, "id", None), "t3_dequeued")
                try:
                    msg_obj = event.message
                    # Buffer immediately — no get_sender() on the hot path.
                    msg = await self._record_message(msg_obj, slot, sender=None)
                    if kind == "new":
                        _lt.mark(msg["id"], "t4_buffered")
                        log.info("MSG slot=%d id=%s group=%r len=%d",
                                 slot + 1, msg["id"], self._group_names[slot],
                                 len(msg["text"]))
                        # Wake the engine scan loop immediately.
                        if self._new_msg_event is not None:
                            self._new_msg_event.set()
                            _lt.mark(msg["id"], "t5_woken")
                        # Resolve sender + write DB off the critical path.
                        t = asyncio.create_task(self._resolve_and_store(msg_obj, msg))
                        self._pending_tasks.add(t)
                        t.add_done_callback(self._pending_tasks.discard)
                except Exception as e:
                    self._add_error(f"Event processor error slot {slot+1}: {e}")
                finally:
                    self._event_queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("Event processor outer error: %s", e)

    async def _backfill(self, slot: int, limit: int = 20) -> int:
        if not self._client or not self._group_ids[slot]:
            return 0
        entity = await self._resolve_entity(slot)
        target = entity or self._group_ids[slot]
        added  = 0
        try:
            async for msg_obj in self._client.iter_messages(target, limit=limit):
                # Backfill is not latency-sensitive — resolve sender synchronously.
                try:
                    sender = await msg_obj.get_sender()
                except Exception:
                    sender = None
                msg = await self._record_message(msg_obj, slot, sender=sender)
                if msg:
                    try:
                        db_module.store_telegram_message(msg)
                    except Exception:
                        pass
                    added += 1
        except Exception as e:
            self._add_error(f"Backfill slot {slot+1} failed: {e}")
        return added

    async def _poll(self, slot: int, interval: float = 1.0, limit: int = 5) -> None:
        self._poller_active[slot]  = True
        self._last_poll_error[slot] = None
        try:
            while self._listener_active[slot] and self._client and self._auth_state == AUTH_CONNECTED:
                self._last_poll_at[slot] = datetime.now(timezone.utc).isoformat()
                await self._backfill(slot=slot, limit=limit)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._last_poll_error[slot] = str(e)
            self._add_error(f"Polling slot {slot+1} failed: {e}")
        finally:
            self._poller_active[slot] = False

    async def _listener_task(self, slot: int) -> None:
        if not self._client or self._auth_state != AUTH_CONNECTED:
            raise RuntimeError("Not connected")
        if not self._group_ids[slot]:
            raise RuntimeError(f"No group selected for slot {slot+1}")

        selected_entity = await self._resolve_entity(slot)
        listener_target = selected_entity or self._group_ids[slot]

        handler      = self._make_message_handler(slot)
        edit_handler = self._make_edit_handler(slot)
        # Remove before adding to avoid doubling handlers if a previous run
        # died before reaching its finally block.
        try:
            self._client.remove_event_handler(handler)
            self._client.remove_event_handler(edit_handler)
        except Exception:
            pass
        self._client.add_event_handler(handler,      events.NewMessage(chats=listener_target))
        self._client.add_event_handler(edit_handler, events.MessageEdited(chats=listener_target))

        self._listener_active[slot] = True
        db_module.save_telegram_reader_event("listener", "started",
                                              f"Slot {slot+1}: {self._group_names[slot]} ({self._group_ids[slot]})")
        log.info("Listener started slot=%d group=%s", slot + 1, self._group_names[slot])
        await self._backfill(slot)

        try:
            # Primary path is the event handler above; poll at 30s as a safety net
            # only (catches any rare missed events), not as the main receive mechanism.
            await self._poll(slot, interval=30.0, limit=3)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self._add_error(f"Listener poller error slot {slot+1}: {e}")
        finally:
            try:
                self._client.remove_event_handler(handler)
                self._client.remove_event_handler(edit_handler)
            except Exception:
                pass
            self._listener_active[slot] = False
            log.info("Listener task finished slot=%d", slot + 1)

