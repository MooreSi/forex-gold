"""Durable storage for the Mac's not-yet-confirmed proposals.

Moved verbatim out of `client.py` -- same methods, same bodies, same names --
so that file stays inside its LOC budget. No behaviour change: `SyncClient`
mixes this in, so `self._pending_settings` and friends resolve exactly as they
did when these were defined inline.

Why any of it exists: `propose_settings()` was once fire-and-forget, so a
change made while the link was down, or dropped before the VPS replied, was
lost silently. On a link that reconnects every 15-90 seconds that was
recurring. Keeping the queue in memory alone was not enough either -- an app
restart, or this object being recreated by a Local/Remote toggle, lost it
permanently with no retry ever happening. Hence app_config.

Covered by tests/core/test_sync_pending_proposals.py.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from backend.src.db import database as db_module

log = logging.getLogger(__name__)


class PendingStoreMixin:
    # ── Pending-settings durability ──────────────────────────────────────────

    @staticmethod
    def _load_pending() -> dict:
        try:
            raw = db_module.get_app_config("sync_pending_settings")
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _persist_pending(self) -> None:
        try:
            db_module.set_app_config("sync_pending_settings", json.dumps(self._pending_settings))
        except Exception as e:
            log.debug("[SyncClient] failed to persist pending settings: %s", e)

    @staticmethod
    def _load_pending_channel_strategy() -> dict:
        try:
            raw = db_module.get_app_config("sync_pending_channel_strategy")
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _persist_pending_channel_strategy(self) -> None:
        try:
            db_module.set_app_config(
                "sync_pending_channel_strategy", json.dumps(self._pending_channel_strategy)
            )
        except Exception as e:
            log.debug("[SyncClient] failed to persist pending channel strategy: %s", e)

    @staticmethod
    def _load_pending_trading_schedule() -> Optional[dict]:
        try:
            raw = db_module.get_app_config("sync_pending_trading_schedule")
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def _persist_pending_trading_schedule(self) -> None:
        try:
            db_module.set_app_config(
                "sync_pending_trading_schedule",
                json.dumps(self._pending_trading_schedule) if self._pending_trading_schedule else "",
            )
        except Exception as e:
            log.debug("[SyncClient] failed to persist pending trading schedule: %s", e)

    @staticmethod
    def _load_pending_strategy_params() -> Optional[dict]:
        try:
            raw = db_module.get_app_config("sync_pending_strategy_params")
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def _persist_pending_strategy_params(self) -> None:
        try:
            db_module.set_app_config(
                "sync_pending_strategy_params",
                json.dumps(self._pending_strategy_params) if self._pending_strategy_params else "",
            )
        except Exception as e:
            log.debug("[SyncClient] failed to persist pending strategy params: %s", e)
