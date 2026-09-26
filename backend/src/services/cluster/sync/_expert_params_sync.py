"""Expert Tunables over the node link, both halves.

`risk/expert_params._forward_over_sync()` has called
`propose_expert_params` / `broadcast_expert_params` since 2026-08-03 behind
`hasattr` guards, and neither method existed, so every Expert Tunables change
stayed on the node it was made on. These are those two methods and what they
need, in the Strategy Parameters shape (client.py / server.py): the whole
snapshot is proposed, held and persisted until the VPS's confirmed snapshot
equals it, re-sent on reconnect, and mirrored down only when nothing is
pending, so a reconnect cannot overwrite an edit that has not landed yet.

Mixins rather than inline because client.py sits at its LOC ceiling.
Pinned by tests/core/test_expert_params_sync.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from backend.src.db import database as db_module
from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, MSG_EXPERT_PARAMS_PROPOSE, MSG_EXPERT_PARAMS_STATE, make,
)

log = logging.getLogger(__name__)

_PENDING_KEY = "sync_pending_expert_params"


class ClientExpertParamsMixin:
    # Class-level defaults: nothing pending, nothing confirmed yet. Set per
    # instance by _init_expert_params_sync().
    remote_expert_params: dict = {}
    _pending_expert_params: Optional[dict] = None

    def _init_expert_params_sync(self) -> None:
        self.remote_expert_params: dict = {}
        self._pending_expert_params: Optional[dict] = self._load_pending_expert_params()

    @staticmethod
    def _load_pending_expert_params() -> Optional[dict]:
        try:
            raw = db_module.get_app_config(_PENDING_KEY)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def _persist_pending_expert_params(self) -> None:
        try:
            db_module.set_app_config(
                _PENDING_KEY,
                json.dumps(self._pending_expert_params) if self._pending_expert_params else "")
        except Exception as e:
            log.debug("[SyncClient] failed to persist pending expert params: %s", e)

    async def propose_expert_params(self, snapshot: dict) -> None:
        self._pending_expert_params = snapshot
        self._persist_pending_expert_params()
        await self._flush_pending_expert_params()

    async def _flush_pending_expert_params(self) -> None:
        if not self._pending_expert_params:
            return
        if self._ws is not None and self.conn_state == CONN_CONNECTED:
            try:
                await self._ws.send(json.dumps(make(
                    MSG_EXPERT_PARAMS_PROPOSE, expert_params=self._pending_expert_params)))
            except Exception as e:
                log.debug("[SyncClient] expert params propose send failed (will "
                          "retry on next reconnect): %s", e)

    def _on_expert_params_state(self, snapshot: Optional[dict], *,
                                connecting: bool = False) -> None:
        """A confirmed snapshot, from the welcome or a broadcast."""
        self.remote_expert_params = dict(snapshot or {})
        if self._pending_expert_params is None:
            if self.remote_expert_params:
                try:
                    from backend.src.services.risk.expert_params import apply_snapshot
                    apply_snapshot(self.remote_expert_params)
                except Exception as e:
                    log.debug("[SyncClient] expert params mirror failed: %s", e)
        elif self._pending_expert_params == self.remote_expert_params:
            self._pending_expert_params = None
            self._persist_pending_expert_params()
        elif connecting:
            log.info("[SyncClient] resending unconfirmed local expert params "
                     "change after reconnect")
            asyncio.create_task(self._flush_pending_expert_params())


class ServerExpertParamsMixin:
    def _expert_params_snapshot(self) -> dict:
        from backend.src.services.risk.expert_params import snapshot
        return snapshot()

    async def _handle_expert_params_propose(self, ws, msg: dict) -> None:
        try:
            from backend.src.services.risk.expert_params import apply_snapshot
            apply_snapshot(msg.get("expert_params") or {})
            log.info("[SyncServer] applied expert params from Mac")
        except Exception as e:
            log.warning("[SyncServer] failed to apply expert params from Mac: %s", e)
            return
        await self.broadcast_expert_params()

    async def broadcast_expert_params(self) -> None:
        await self._broadcast(make(
            MSG_EXPERT_PARAMS_STATE, expert_params=self._expert_params_snapshot()))
