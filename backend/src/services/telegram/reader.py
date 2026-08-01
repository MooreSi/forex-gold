"""
TelegramReader — Telethon auth state machine + group listener.
Extracted from telegram_reader/service.py with FastAPI layer removed.
Callers call methods directly; no HTTP indirection.

M2 file-size split: the class body now composes _AuthMixin (auth flow,
session restore, reconnect, watchdog) and _ListenerMixin (groups,
listeners, message pipeline) from reader_auth.py / reader_listener.py;
shared constants live in reader_common.py and are re-exported here so
existing importers keep working unchanged.
"""

from backend.src.services.telegram.reader_common import (  # noqa: F401
    _TELETHON_AVAILABLE, _NUM_SLOTS, _media_type, _safe_phone_log,
    AUTH_DISCONNECTED, AUTH_AWAITING_CODE, AUTH_AWAITING_2FA,
    AUTH_CONNECTED, AUTH_RECONNECTING, AUTH_FAILED,
)
from backend.src.services.telegram.reader_auth import _AuthMixin
from backend.src.services.telegram.reader_listener import _ListenerMixin

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

from backend.src.db import database as db_module
from backend.src.services.telegram import repo as telegram_repo

if _TELETHON_AVAILABLE:
    from backend.src.services.telegram.reader_common import (
        TelegramClient, ConnectionTcpAbridged,
    )

log = logging.getLogger(__name__)


class TelegramReader(_AuthMixin, _ListenerMixin):
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
            total = telegram_repo.count_telegram_messages()
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

