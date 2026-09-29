"""EA templates over the node link: the Mac's library reaches the VPS (owner, 2026-09-29).

"always sync the templates to the VPS ... if new templates are created or
changed on the local node they should be synced to the vps". Found that day:
the VPS's `ea_trade_templates` was empty while its channels and the Reversal
Engine traded `template:30 TP1 SL50 and Trail`, a template only the Mac had.
38 trades ran with no partial-close ladder.

The Mac is the source. It sends the whole library, in the export shape
(`ea_templates.export_templates`: name plus every editable field), on
connect, within `_CHECK_EVERY_S` of any change, and every `_RESEND_EVERY_S`
regardless. Change is noticed by comparing a digest of the library rather
than by hooking each writer: templates are written by the Templates page, the
EA panel, presets, rename and import, and a hook missed on one of them is how
the next divergence would start. No ack: a send lost with the connection is
repeated on the reconnect.

The VPS writes only what differs from its own copy (`save_ea_template`, so the
same cleaning as a local save), and keeps a template the Mac did not send: an
extra template never makes an order run without its ladder, and deleting one a
VPS channel names would refuse that channel's orders
(services/broker/template_presence.py). An entry without a name, or a payload
that is not a list, writes nothing at all.

Pinned by tests/core/test_ea_templates_sync.py.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time

from backend.src.db import database as db_module
from backend.src.services.broker import ea_templates
from backend.src.services.cluster.sync.protocol import CONN_CONNECTED, MSG_EA_TEMPLATES, make

log = logging.getLogger(__name__)

_CHECK_EVERY_S = 5.0
_RESEND_EVERY_S = 600.0


def library_snapshot() -> list[dict]:
    """This node's templates, sorted by name, in the export shape."""
    rows = json.loads(ea_templates.export_templates())["templates"]
    return sorted(rows, key=lambda t: t["name"])


def _digest(templates: list[dict]) -> str:
    return hashlib.sha256(json.dumps(templates, sort_keys=True).encode()).hexdigest()


def apply_library(templates) -> dict:
    """The VPS's side. Returns {"written": [names], "error": str}."""
    if not isinstance(templates, list) or not all(
            isinstance(t, dict) and str(t.get("name") or "").strip() for t in templates):
        return {"written": [], "error": "not a list of named templates"}
    mine = {t["name"]: t for t in library_snapshot()}
    written = []
    for t in templates:
        name = str(t["name"]).strip()
        fields = {k: t[k] for k in ea_templates.DEFAULTS if k in t}
        have = mine.get(name)
        if have is not None and all(have.get(k) == v for k, v in fields.items()):
            continue
        ea_templates.save_ea_template(name, fields)
        written.append(name)
    return {"written": written, "error": ""}


class ClientEaTemplatesMixin:
    async def _push_ea_templates_if_changed(self) -> bool:
        """Send the library if it changed, or is due a resend. True if sent."""
        if getattr(self, "_ws", None) is None or self.conn_state != CONN_CONNECTED:
            return False
        snap = await db_module.to_db_thread(library_snapshot)
        digest = _digest(snap)
        now = time.monotonic()
        last_digest, last_at = self.__dict__.get("_ea_templates_sent", (None, 0.0))
        if digest == last_digest and now - last_at < _RESEND_EVERY_S:
            return False
        await self._ws.send(json.dumps(make(MSG_EA_TEMPLATES, templates=snap)))
        self._ea_templates_sent = (digest, now)
        return True

    async def _ea_templates_sync_loop(self) -> None:
        self._ea_templates_sent = (None, 0.0)  # a new connection always sends
        while self.conn_state == CONN_CONNECTED:
            try:
                await self._push_ea_templates_if_changed()
            except Exception as e:
                log.warning("[SyncClient] EA template send failed (resent on reconnect): %s", e)
                break
            await asyncio.sleep(_CHECK_EVERY_S)


class ServerEaTemplatesMixin:
    async def _handle_ea_templates(self, ws, msg: dict) -> None:
        try:
            result = await db_module.to_db_thread(apply_library, msg.get("templates"))
        except Exception as e:
            log.error("[SyncServer] applying the Mac's EA templates failed: %s", e)
            return
        if result["error"]:
            log.error("[SyncServer] refused the Mac's EA templates: %s", result["error"])
        elif result["written"]:
            log.info("[SyncServer] EA templates from the Mac: wrote %s", result["written"])
