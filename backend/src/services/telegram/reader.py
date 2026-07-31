"""
TelegramReader — Telethon auth state machine + group listener.
Extracted from telegram_reader/service.py with FastAPI layer removed.
Callers call methods directly; no HTTP indirection.
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

from forex_trader.core import database as db_module

log = logging.getLogger(__name__)

try:
    from telethon import TelegramClient, events
    from telethon.errors import (
        ApiIdInvalidError,
        FloodWaitError,
        PhoneCodeExpiredError,
        PhoneCodeInvalidError,
        PhoneNumberInvalidError,
        PasswordHashInvalidError,
        SessionPasswordNeededError,
    )
    from telethon.network import ConnectionTcpAbridged
    from telethon.tl.types import Channel, User
    _TELETHON_AVAILABLE = True
except ImportError:
    _TELETHON_AVAILABLE = False
    log.warning("Telethon not installed — Telegram reader will not function")

# Auth states
AUTH_DISCONNECTED  = "disconnected"
AUTH_AWAITING_CODE = "awaiting_code"
AUTH_AWAITING_2FA  = "awaiting_2fa"
AUTH_CONNECTED     = "connected"
AUTH_RECONNECTING  = "reconnecting"
AUTH_FAILED        = "failed"

_NUM_SLOTS = 3


def _safe_phone_log(phone: Optional[str]) -> str:
    if not phone:
        return "***"
    digits = re.sub(r"\D", "", phone)
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"


def _media_type(msg_obj) -> str:
    if not hasattr(msg_obj, "media") or msg_obj.media is None:
        return "none"
    cls = type(msg_obj.media).__name__.lower()
    if "photo"    in cls: return "photo"
    if "document" in cls: return "document"
    if "video"    in cls: return "video"
    if "audio"    in cls: return "audio"
    return "unknown"


class TelegramReader:
    def __init__(self, config: dict):
        self._cfg            = config
        self._sessions_dir   = config.get("sessions_dir", "./data/sessions")
        self._session_name   = config.get("telegram_session_name", "forex_trader")
        os.makedirs(self._sessions_dir, exist_ok=True)

        # Auth state
        self._auth_state:      str           = AUTH_DISCONNECTED
        self._auth_error:      Optional[str] = None
        self._phone_code_hash: Optional[str] = None
        self._runtime_phone:   Optional[str] = None
        self._client: Optional["TelegramClient"] = None

        # Slot state (_NUM_SLOTS slots)
        self._group_ids:      list[Optional[int]]  = [None] * _NUM_SLOTS
        self._group_names:    list[Optional[str]]  = [None] * _NUM_SLOTS
        self._group_entities: list                 = [None] * _NUM_SLOTS
        self._listener_active: list[bool]          = [False] * _NUM_SLOTS
        self._listener_tasks:  list                = [None] * _NUM_SLOTS
        self._poller_active:   list[bool]          = [False] * _NUM_SLOTS
        self._last_poll_at:    list[Optional[str]] = [None] * _NUM_SLOTS
        self._last_poll_error: list[Optional[str]] = [None] * _NUM_SLOTS
        self._pending_tasks:   set                 = set()

        # Stats
        self._messages_session: int = 0
        self._last_message_at:  Optional[str] = None
        self._recent_errors:    list[str] = []

        # In-memory buffer (newest first)
        self._MSG_BUFFER = 250
        self._msg_buffer: list[dict] = []

        # Background tasks
        self._watchdog_task: Optional[asyncio.Task] = None
        self._last_ping_at:  Optional[float]        = None  # monotonic time of last get_me() ping

        # Step 5: queue-based handler — handler enqueues raw events, processor
        # buffers immediately; get_sender() and DB write run in a background task.
        self._event_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._processor_task: Optional[asyncio.Task] = None
        # Set when a new message is buffered — lets the engine scan loop wake
        # immediately rather than waiting up to 1 second for its poll interval.
        self._new_msg_event: Optional[asyncio.Event] = None

    # ── Startup ───────────────────────────────────────────────────────────────

    async def startup(self) -> None:
        if not _TELETHON_AVAILABLE:
            log.warning("Telethon unavailable — Telegram reader disabled")
            return
        self._new_msg_event = asyncio.Event()
        self._processor_task = asyncio.create_task(self._event_processor())
        self._pending_tasks.add(self._processor_task)
        self._processor_task.add_done_callback(self._pending_tasks.discard)
        reconnected = await self._try_restore_session()
        if reconnected:
            await self._restore_listeners()
        self._watchdog_task = asyncio.create_task(self._connection_watchdog())
        self._pending_tasks.add(self._watchdog_task)
        self._watchdog_task.add_done_callback(self._pending_tasks.discard)
        log.info("TelegramReader started (state=%s)", self._auth_state)

    async def shutdown(self) -> None:
        self._stop_all_listeners()
        for t in list(self._pending_tasks):
            t.cancel()
        if self._processor_task and not self._processor_task.done():
            self._processor_task.cancel()
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        log.info("TelegramReader stopped")

    # ── Session helpers ───────────────────────────────────────────────────────

    def _session_path(self, name: str) -> str:
        return os.path.join(self._sessions_dir, name)

    def _session_exists(self) -> bool:
        return os.path.exists(self._session_path(self._session_name) + ".session")

    def _make_client(self, api_id: int, api_hash: str) -> "TelegramClient":
        return TelegramClient(
            self._session_path(self._session_name),
            api_id, api_hash,
            connection=ConnectionTcpAbridged,
            connection_retries=10,
            auto_reconnect=True,
            timeout=15,
            system_version="4.16.30-vxCUSTOM",
        )

    def _get_api_id(self) -> Optional[int]:
        raw = self._cfg.get("telegram_api_id", "")
        try:
            return int(str(raw).strip()) if str(raw).strip().isdigit() else None
        except Exception:
            return None

    def _get_api_hash(self) -> Optional[str]:
        raw = str(self._cfg.get("telegram_api_hash", "") or "").strip()
        return raw or None

    # ── Properties for UI ─────────────────────────────────────────────────────

    @property
    def auth_state(self) -> str:
        return self._auth_state

    @property
    def auth_error(self) -> Optional[str]:
        return self._auth_error

    @property
    def telethon_available(self) -> bool:
        return _TELETHON_AVAILABLE

    def get_status(self) -> dict:
        try:
            with db_module.db() as conn:
                total = conn.execute("SELECT COUNT(*) FROM telegram_messages").fetchone()[0]
        except Exception:
            total = 0
        slots = [
            {
                "slot":              s + 1,
                "group_id":          self._group_ids[s],
                "group_name":        self._group_names[s],
                "listener_active":   self._listener_active[s],
                "poller_active":     self._poller_active[s],
                "last_poll_at":      self._last_poll_at[s],
                "last_poll_error":   self._last_poll_error[s],
            }
            for s in range(_NUM_SLOTS)
        ]
        return {
            "auth_state":         self._auth_state,
            "auth_error":         self._auth_error,
            "telethon_available": _TELETHON_AVAILABLE,
            "session_exists":     self._session_exists(),
            "api_id_set":         self._get_api_id() is not None,
            "api_hash_set":       self._get_api_hash() is not None,
            "messages_this_session": self._messages_session,
            "messages_stored_total": total,
            "last_message_at":    self._last_message_at,
            "recent_errors":      self._recent_errors,
            "slots":              slots,
        }

    def get_buffer_messages(self, limit: int = 100,
                             group_id: Optional[str] = None) -> list[dict]:
        msgs = self._msg_buffer if not group_id else [
            m for m in self._msg_buffer if str(m.get("group_id", "")) == str(group_id)
        ]
        return msgs[:limit]

    async def wait_for_new_message(self, timeout: float = 1.0) -> bool:
        """Block until a new message is buffered or timeout elapses.
        Returns True if woken by a message, False on timeout.
        Used by the engine scan loop to eliminate the fixed 1-second poll delay.
        """
        if self._new_msg_event is None:
            await asyncio.sleep(timeout)
            return False
        try:
            await asyncio.wait_for(self._new_msg_event.wait(), timeout=timeout)
            self._new_msg_event.clear()
            return True
        except asyncio.TimeoutError:
            return False

    def get_active_group_slots(self) -> dict[str, int]:
        return {str(gid): s + 1 for s, gid in enumerate(self._group_ids) if gid is not None}

    def get_group_name(self, group_id: str) -> Optional[str]:
        for s, gid in enumerate(self._group_ids):
            if str(gid or "") == str(group_id):
                return self._group_names[s]
        return None

    async def get_dc_info(self) -> dict:
        """
        Step 1 diagnostic: return the session DC and the DC of each
        connected channel entity.  A mismatch means Telegram proxies
        every update through an extra hop, adding ~100ms.
        """
        if not self._client or self._auth_state != AUTH_CONNECTED:
            return {"error": "Not connected"}
        session_dc = getattr(self._client.session, "dc_id", None)
        channels = []
        for s in range(_NUM_SLOTS):
            entity = self._group_entities[s]
            if entity is None:
                continue
            ch_dc = None
            photo = getattr(entity, "photo", None)
            if photo:
                ch_dc = getattr(photo, "dc_id", None)
            channels.append({
                "slot":         s + 1,
                "group_name":   self._group_names[s],
                "group_id":     self._group_ids[s],
                "channel_dc":   ch_dc,
                "mismatch":     ch_dc is not None and ch_dc != session_dc,
            })
        result = {"session_dc": session_dc, "channels": channels}
        mismatches = [c for c in channels if c["mismatch"]]
        if mismatches:
            log.warning(
                "DC mismatch: session is on DC%s but channel(s) %s are on DC%s — "
                "each update hops via extra DC; consider migrating session to DC%s",
                session_dc,
                [c["group_name"] for c in mismatches],
                [c["channel_dc"] for c in mismatches],
                mismatches[0]["channel_dc"],
            )
        else:
            log.info("DC check: session DC%s matches all channels — optimal", session_dc)
        return result

    # ── Auth flow ─────────────────────────────────────────────────────────────

    async def send_code(self, phone: str,
                         api_id_override: Optional[int] = None,
                         api_hash_override: Optional[str] = None) -> dict:
        if not _TELETHON_AVAILABLE:
            return {"error": "Telethon not installed"}

        api_id   = api_id_override or self._get_api_id()
        api_hash = api_hash_override or self._get_api_hash()
        if not api_id:
            return {"error": "Telegram API ID is required"}
        if not api_hash:
            return {"error": "Telegram API Hash is required"}
        if not phone:
            return {"error": "Phone number is required"}

        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None

        try:
            client = self._make_client(api_id, api_hash)
            await client.connect()
            if await client.is_user_authorized():
                self._client       = client
                self._auth_state   = AUTH_CONNECTED
                self._auth_error   = None
                self._runtime_phone = phone
                db_module.save_telegram_reader_event("auth", "connected", "Existing session reused")
                return {"auth_state": AUTH_CONNECTED, "message": "Already authenticated — session reused"}
            result = await client.send_code_request(phone)
            self._client          = client
            self._phone_code_hash = result.phone_code_hash
            self._runtime_phone   = phone
            self._auth_state      = AUTH_AWAITING_CODE
            self._auth_error      = None
            db_module.save_telegram_reader_event("auth", "awaiting_code",
                                                 f"Code sent to {_safe_phone_log(phone)}")
            log.info("Code sent to %s", _safe_phone_log(phone))
            return {"auth_state": AUTH_AWAITING_CODE, "message": "Login code sent to your Telegram app"}
        except PhoneNumberInvalidError:
            self._auth_state = AUTH_FAILED
            self._auth_error = "Invalid phone number"
            return {"error": "Invalid phone number"}
        except ApiIdInvalidError:
            self._auth_state = AUTH_FAILED
            self._auth_error = "Invalid API ID or API Hash"
            return {"error": "Invalid API ID or API Hash"}
        except FloodWaitError as e:
            self._auth_state = AUTH_FAILED
            self._auth_error = f"Rate limited — wait {e.seconds}s"
            return {"error": f"Telegram rate limit — wait {e.seconds} seconds"}
        except Exception as e:
            self._auth_state = AUTH_FAILED
            self._auth_error = str(e)
            return {"error": str(e)}

    async def verify_code(self, code: str) -> dict:
        if not _TELETHON_AVAILABLE:
            return {"error": "Telethon not installed"}
        if self._auth_state != AUTH_AWAITING_CODE:
            return {"error": f"Not awaiting code (state={self._auth_state})"}
        if not self._client or not self._phone_code_hash or not self._runtime_phone:
            return {"error": "Auth session lost — restart from send-code"}
        code = str(code).strip()
        if not code:
            return {"error": "Code is required"}
        try:
            await self._client.sign_in(self._runtime_phone, code,
                                        phone_code_hash=self._phone_code_hash)
            self._auth_state = AUTH_CONNECTED
            self._auth_error = None
            db_module.save_telegram_reader_event("auth", "connected", "Code verified successfully")
            log.info("Code verified")
            return {"auth_state": AUTH_CONNECTED, "message": "Authenticated successfully"}
        except SessionPasswordNeededError:
            self._auth_state = AUTH_AWAITING_2FA
            self._auth_error = None
            return {"auth_state": AUTH_AWAITING_2FA, "message": "2FA password required"}
        except PhoneCodeInvalidError:
            self._auth_error = "Incorrect login code"
            return {"error": "Incorrect login code"}
        except PhoneCodeExpiredError:
            self._auth_state = AUTH_DISCONNECTED
            self._auth_error = "Login code expired"
            return {"error": "Login code expired — restart authentication"}
        except Exception as e:
            self._auth_state = AUTH_FAILED
            self._auth_error = str(e)
            return {"error": str(e)}

    async def verify_2fa(self, password: str) -> dict:
        if not _TELETHON_AVAILABLE:
            return {"error": "Telethon not installed"}
        if self._auth_state != AUTH_AWAITING_2FA:
            return {"error": f"Not awaiting 2FA (state={self._auth_state})"}
        password = password or self._cfg.get("telegram_2fa_password", "")
        if not password:
            return {"error": "2FA password required"}
        try:
            await self._client.sign_in(password=password)
            self._auth_state = AUTH_CONNECTED
            self._auth_error = None
            db_module.save_telegram_reader_event("auth", "connected", "2FA verified")
            log.info("2FA verified")
            return {"auth_state": AUTH_CONNECTED, "message": "2FA verified — authenticated"}
        except PasswordHashInvalidError:
            self._auth_error = "Incorrect 2FA password"
            return {"error": "Incorrect 2FA password"}
        except Exception as e:
            self._auth_state = AUTH_FAILED
            self._auth_error = str(e)
            return {"error": str(e)}

    async def disconnect(self) -> None:
        self._stop_all_listeners()
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        self._auth_state      = AUTH_DISCONNECTED
        self._auth_error      = None
        self._phone_code_hash = None
        self._runtime_phone   = None
        db_module.save_telegram_reader_event("auth", "disconnected", "Manually disconnected")

    async def reset_session(self) -> None:
        self._reset_in_progress = True
        try:
            await self.disconnect()
            session_file = self._session_path(self._session_name) + ".session"
            if os.path.exists(session_file):
                try:
                    os.remove(session_file)
                    log.info("Session file deleted")
                except Exception as e:
                    log.error("Could not delete session file: %s", e)
            db_module.save_telegram_reader_event("auth", "reset", "Session reset and deleted")
        finally:
            self._reset_in_progress = False

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

    # ── Session restore ───────────────────────────────────────────────────────

    async def _try_restore_session(self) -> bool:
        api_id   = self._get_api_id()
        api_hash = self._get_api_hash()
        if not api_id or not api_hash or not self._session_exists():
            return False
        try:
            log.info("Session file found — attempting auto-reconnect")
            self._auth_state = AUTH_RECONNECTING
            client = self._make_client(api_id, api_hash)
            await client.connect()
            if await client.is_user_authorized():
                self._client     = client
                self._auth_state = AUTH_CONNECTED
                self._auth_error = None
                me = await client.get_me()
                _session_dc = getattr(client.session, "dc_id", "?")
                log.info("Auto-reconnected as user id=%s (session DC=%s)",
                         me.id if me else "?", _session_dc)
                db_module.save_telegram_reader_event("auth", "connected",
                                                      f"Auto-reconnected from saved session (DC{_session_dc})")
                return True
            else:
                await client.disconnect()
                self._auth_state = AUTH_DISCONNECTED
                return False
        except Exception as e:
            self._auth_state = AUTH_DISCONNECTED
            self._auth_error = str(e)
            log.warning("Auto-reconnect failed: %s", e)
            return False

    async def _restore_listeners(self) -> None:
        """Restart listeners for any previously selected groups stored in DB."""
        try:
            with db_module.db() as conn:
                row = conn.execute(
                    "SELECT value FROM app_config WHERE key='selected_groups'"
                ).fetchone()
            if not row:
                return
            saved = json.loads(row[0])
            for item in saved:
                if not item or not item.get("group_id"):
                    continue
                # New format has explicit 'slot' key; old format uses list position
                if "slot" in item:
                    s = int(item["slot"]) - 1          # 1-indexed → 0-indexed
                else:
                    s = saved.index(item)              # legacy: position = slot
                if not (0 <= s < _NUM_SLOTS):
                    continue
                self._group_ids[s]   = int(item["group_id"])
                self._group_names[s] = item.get("group_name", "")
                log.info("Restoring listener slot=%d group=%s (%s)",
                         s + 1, self._group_names[s], self._group_ids[s])
                try:
                    await self._resolve_entity(s)
                    task = asyncio.create_task(self._listener_task(s))
                    self._listener_tasks[s] = task
                    self._pending_tasks.add(task)
                    task.add_done_callback(self._pending_tasks.discard)
                    await asyncio.sleep(0.2)
                except Exception as e:
                    log.warning("Auto-restore listener slot=%d failed: %s", s + 1, e)
            # Step 1/2: log DC alignment after all entities are resolved
            try:
                await self.get_dc_info()
            except Exception:
                pass
        except Exception as e:
            log.warning("Could not restore group selections: %s", e)

    def save_group_selections(self) -> None:
        # Always save all slots with explicit slot index so restore maps correctly
        data = [
            {
                "slot":       s + 1,
                "group_id":   self._group_ids[s],
                "group_name": self._group_names[s],
            }
            for s in range(_NUM_SLOTS)
        ]
        try:
            with db_module.db() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO app_config (key,value) VALUES ('selected_groups',?)",
                    (json.dumps(data),),
                )
            log.info("Group selections saved: %s",
                     [(d["slot"], d["group_name"]) for d in data if d["group_id"]])
        except Exception as e:
            log.warning("Could not save group selections: %s", e)

    # ── Reconnect ─────────────────────────────────────────────────────────────

    async def _reconnect(self) -> bool:
        api_id   = self._get_api_id()
        api_hash = self._get_api_hash()
        if not api_id or not api_hash or not self._session_exists():
            self._auth_state = AUTH_DISCONNECTED
            self._auth_error = "No session file — re-authenticate"
            return False
        try:
            if self._client:
                try:
                    await self._client.disconnect()
                except Exception:
                    pass
            self._auth_state = AUTH_RECONNECTING
            client = self._make_client(api_id, api_hash)
            await client.connect()
            if await client.is_user_authorized():
                self._client     = client
                self._auth_state = AUTH_CONNECTED
                self._auth_error = None
                _session_dc = getattr(client.session, "dc_id", "?")
                log.info("Reconnected successfully (session DC=%s)", _session_dc)
                db_module.save_telegram_reader_event("auth", "reconnected",
                                                      f"Auto-reconnected after connection drop (DC{_session_dc})")
                return True
            else:
                await client.disconnect()
                self._auth_state = AUTH_DISCONNECTED
                self._auth_error = "Session expired — re-authenticate"
                return False
        except Exception as e:
            self._auth_state = AUTH_DISCONNECTED
            self._auth_error = str(e)
            log.warning("Reconnect failed: %s", e)
            return False

    async def reconnect(self) -> dict:
        if getattr(self, "_reset_in_progress", False):
            return {"error": "Session reset in progress — try again shortly"}
        if not self._session_exists():
            return {"error": "No session file — authenticate first"}
        self._stop_all_listeners()
        await asyncio.sleep(0)  # yield so cancelled listener tasks reach their finally blocks
        ok = await self._reconnect()
        if not ok:
            return {"error": f"Reconnect failed: {self._auth_error}"}
        for s in range(_NUM_SLOTS):
            if self._group_ids[s]:
                await self._resolve_entity(s)
                task = asyncio.create_task(self._listener_task(s))
                self._listener_tasks[s] = task
                self._pending_tasks.add(task)
                task.add_done_callback(self._pending_tasks.discard)
        return {"auth_state": self._auth_state}

    # ── Watchdog ──────────────────────────────────────────────────────────────

    async def _connection_watchdog(self) -> None:
        await asyncio.sleep(30)
        while True:
            try:
                if getattr(self, "_reset_in_progress", False):
                    await asyncio.sleep(15)
                    continue
                if self._auth_state == AUTH_CONNECTED and self._client:
                    if not self._client.is_connected():
                        log.warning("Watchdog: client dropped — reconnecting")
                        self._stop_all_listeners()
                        ok = await self._reconnect()
                        if ok:
                            for s in range(_NUM_SLOTS):
                                if self._group_ids[s]:
                                    await self._resolve_entity(s)
                                    task = asyncio.create_task(self._listener_task(s))
                                    self._listener_tasks[s] = task
                                    self._pending_tasks.add(task)
                                    task.add_done_callback(self._pending_tasks.discard)
                    else:
                        # ── Keepalive ping (every 5 min) ─────────────────────
                        # is_connected() only checks an in-memory flag; a stale
                        # TCP connection can appear "connected" while Telegram
                        # has silently closed the socket.  get_me() forces a
                        # real round-trip that will fail on a zombie connection.
                        _PING_INTERVAL = 300  # seconds
                        now_mono = time.monotonic()
                        if (self._last_ping_at is None
                                or now_mono - self._last_ping_at >= _PING_INTERVAL):
                            try:
                                await self._client.get_me()
                                self._last_ping_at = now_mono
                                log.debug("Watchdog: keepalive ping OK")
                            except Exception as ping_err:
                                log.warning(
                                    "Watchdog: keepalive ping failed — treating as dropped: %s",
                                    ping_err,
                                )
                                self._last_ping_at = None
                                self._stop_all_listeners()
                                ok = await self._reconnect()
                                if ok:
                                    for s in range(_NUM_SLOTS):
                                        if self._group_ids[s]:
                                            await self._resolve_entity(s)
                                            new_task = asyncio.create_task(
                                                self._listener_task(s)
                                            )
                                            self._listener_tasks[s] = new_task
                                            self._pending_tasks.add(new_task)
                                            new_task.add_done_callback(
                                                self._pending_tasks.discard
                                            )
                                # Skip the per-slot dead-listener check this
                                # cycle; we just restarted everything above.
                                await asyncio.sleep(15)
                                continue

                        # Client connected — check each slot's listener is alive
                        for s in range(_NUM_SLOTS):
                            if not self._group_ids[s]:
                                continue
                            task = self._listener_tasks[s]
                            if (task is None or task.done()) and not self._listener_active[s]:
                                log.warning(
                                    "Watchdog: listener slot=%d dead — restarting", s + 1
                                )
                                try:
                                    self._group_entities[s] = None  # force re-resolve
                                    await self._resolve_entity(s)
                                    new_task = asyncio.create_task(self._listener_task(s))
                                    self._listener_tasks[s] = new_task
                                    self._pending_tasks.add(new_task)
                                    new_task.add_done_callback(self._pending_tasks.discard)
                                    await asyncio.sleep(0.2)
                                except Exception as e:
                                    log.warning(
                                        "Watchdog: failed to restart slot=%d: %s", s + 1, e
                                    )
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("Watchdog error: %s", e)
            await asyncio.sleep(15)  # check every 15s instead of 30s

    def _add_error(self, msg: str) -> None:
        self._recent_errors = ([msg] + self._recent_errors)[:10]
