"""Auth flow, session restore, reconnect and the connection watchdog -- split verbatim from reader.py (M2 file-size
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


class _AuthMixin:
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
            row = telegram_repo.get_selected_groups_json()
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
