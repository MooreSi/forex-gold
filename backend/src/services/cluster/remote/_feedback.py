"""The feedback messages on the remote connection, both ends.

Kept out of `client.py` and `server.py`, which are baselined at their line
counts: each gains a one-line hook that calls in here.
"""
from __future__ import annotations

import json
import logging

from backend.src.services.cluster.remote.protocol import make
from backend.src.services.feedback import outbox, service

log = logging.getLogger(__name__)

MSG_FEEDBACK = "feedback"          # client -> server: {"entry": {...}}
MSG_FEEDBACK_ACK = "feedback_ack"  # server -> client: {"id": "..."}


async def flush(ws) -> None:
    """Client: send everything not yet acknowledged."""
    for entry in outbox.pending():
        await ws.send(json.dumps(make(MSG_FEEDBACK, entry=entry)))


def on_ack(msg: dict) -> None:
    """Client: the issuer has it, stop re-sending."""
    outbox.remove(str(msg.get("id") or ""))


async def handle(ws, token_meta: dict, hostname: str, ip: str, msg: dict) -> None:
    """Server: store, announce, ack.

    A malformed entry is acked as well. The client keeps anything un-acked and
    would otherwise resend the same bad message on every connection for ever.
    """
    raw = msg.get("entry") if isinstance(msg.get("entry"), dict) else {}
    try:
        entry = service.clean(raw)
        entry["hostname"] = entry["hostname"] or hostname
        entry["name"] = entry["name"] or str(token_meta.get("name") or "")
        entry["email"] = entry["email"] or str(token_meta.get("email") or "")
        entry["ip"] = ip
        await service.receive(entry)
    except Exception as exc:                      # noqa: BLE001
        log.warning("[RemoteServer] Feedback from %s rejected: %s", hostname, exc)
    await ws.send(json.dumps(make(MSG_FEEDBACK_ACK, id=str(raw.get("id") or ""))))
