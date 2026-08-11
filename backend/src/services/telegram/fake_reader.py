"""FakeTelegramReader — scripted signals into the real pipeline, offline.

Replays Telegram-shaped messages (from a scenario dict — see
tools/debug_scenarios/) into the same newest-first buffer contract the
Telethon reader fills, so runtime._scan_messages → parser → signal row
runs completely unmodified. A fake that bypassed the parser would prove
nothing about the system.

Surface: exactly the subset the runtime, scan pipeline and status panels
consume — pinned by tests/services/telegram/test_fake_reader.py. No
Telethon, no network, no credentials.

Selection happens at the composition root (backend.src.app) when
config.is_debug(); this module never decides that itself.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

_FAKE_GROUP_BASE = 900000


class FakeTelegramReader:
    def __init__(self, config: dict, scenario: Optional[dict] = None):
        self._cfg = config
        signals = (scenario or {}).get("signals") or []
        # channel name → synthetic group id, in first-seen order
        self._channels: dict[str, int] = {}
        self._script: list[dict] = []
        for i, item in enumerate(signals):
            channel = str(item.get("channel") or "Debug Channel")
            gid = self._channels.setdefault(channel, _FAKE_GROUP_BASE + len(self._channels) + 1)
            self._script.append({
                "at": float(item.get("at") or 0.0),
                "channel": channel,
                "group_id": gid,
                "text": str(item.get("text") or ""),
                "id": 1000 + i,
            })
        if not self._channels:
            self._channels["Debug Channel"] = _FAKE_GROUP_BASE + 1
        self._script.sort(key=lambda s: s["at"])
        self._started_at: Optional[float] = None
        self._released = 0
        self._msg_buffer: list[dict] = []
        self._messages_session = 0
        self._last_message_at: Optional[str] = None
        self._new_msg_event: Optional[asyncio.Event] = None
        self._replay_task: Optional[asyncio.Task] = None

    # ── Feeding ───────────────────────────────────────────────────────────

    def feed_due(self, now: Optional[float] = None) -> int:
        """Buffer every scripted message whose time has come. `now` is
        seconds since start (tests pass it explicitly; the replay task uses
        the wall clock). Returns how many were released."""
        if now is None:
            if self._started_at is None:
                return 0
            now = _time.time() - self._started_at
        released = 0
        while self._released < len(self._script) and self._script[self._released]["at"] <= now:
            item = self._script[self._released]
            self._released += 1
            released += 1
            stamp = datetime.now(timezone.utc).isoformat()
            self._msg_buffer.insert(0, {
                "id": item["id"],
                "group_id": item["group_id"],
                "group_name": item["channel"],
                "text": item["text"],
                "timestamp": stamp,
                "received_at": stamp,
                "sender": "debug-script",
                "media_type": None,
                "is_forwarded": False,
            })
            self._messages_session += 1
            self._last_message_at = stamp
            if self._new_msg_event is not None:
                self._new_msg_event.set()
        return released

    async def _replay_loop(self) -> None:
        while self._released < len(self._script):
            self.feed_due()
            await asyncio.sleep(0.5)

    # ── Lifecycle ─────────────────────────────────────────────────────────

    async def startup(self) -> None:
        self._new_msg_event = asyncio.Event()
        self._started_at = _time.time()
        self._replay_task = asyncio.create_task(self._replay_loop())
        log.info("FakeTelegramReader started — %d scripted message(s), channels: %s",
                 len(self._script), list(self._channels))

    async def shutdown(self) -> None:
        if self._replay_task is not None and not self._replay_task.done():
            self._replay_task.cancel()
        log.info("FakeTelegramReader stopped")

    # ── The consumed surface ──────────────────────────────────────────────

    @property
    def auth_state(self) -> str:
        return "CONNECTED"

    @property
    def auth_error(self) -> Optional[str]:
        return None

    @property
    def telethon_available(self) -> bool:
        return False  # honest: there is no Telethon session behind this

    def get_buffer_messages(self, limit: int = 100,
                            group_id: Optional[str] = None) -> list[dict]:
        msgs = self._msg_buffer if not group_id else [
            m for m in self._msg_buffer if str(m.get("group_id", "")) == str(group_id)
        ]
        return msgs[:limit]

    async def wait_for_new_message(self, timeout: float = 1.0) -> bool:
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
        return {str(gid): slot + 1 for slot, gid in enumerate(self._channels.values())}

    def get_group_name(self, group_id: str) -> Optional[str]:
        for name, gid in self._channels.items():
            if str(gid) == str(group_id):
                return name
        return None

    def get_status(self) -> dict:
        slots = [
            {"slot": s + 1, "group_id": gid, "group_name": name,
             "listener_active": True, "poller_active": False,
             "last_poll_at": None, "last_poll_error": None}
            for s, (name, gid) in enumerate(self._channels.items())
        ]
        return {
            "auth_state": self.auth_state, "auth_error": None,
            "telethon_available": False, "session_exists": True,
            "api_id_set": True, "api_hash_set": True,
            "messages_this_session": self._messages_session,
            "messages_stored_total": self._messages_session,
            "last_message_at": self._last_message_at,
            "recent_errors": [], "slots": slots,
            "debug_fake": True,
        }
