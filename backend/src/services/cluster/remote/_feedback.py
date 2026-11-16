"""The feedback messages on the remote connection, both ends.

Kept out of `client.py` and `server.py`, which are baselined at their line
counts: each gains a one-line hook that calls in here.
"""
from __future__ import annotations

import json
import logging

from backend.src.services.cluster.remote.protocol import make
from backend.src.services.feedback import outbox, sent, service, store

log = logging.getLogger(__name__)

MSG_FEEDBACK = "feedback"          # client -> server: {"entry": {...}}
MSG_FEEDBACK_ACK = "feedback_ack"  # server -> client: {"id": "..."}
MSG_FEEDBACK_ASK = "feedback_ask"  # client -> server: {"ids": [...]} still awaiting
MSG_FEEDBACK_DONE = "feedback_done"  # server -> client: {"ids": [...]} now Completed

#: What each end's one-line hook dispatches on.
SERVER_TYPES = (MSG_FEEDBACK, MSG_FEEDBACK_ASK)
CLIENT_TYPES = (MSG_FEEDBACK_ACK, MSG_FEEDBACK_DONE)
#: Ids asked about per ping. A sender with more open feedback than this hears
#: about the rest on later pings, as the first ones complete.
MAX_ASK = 200


async def flush(ws) -> None:
    """Client: send everything not yet acknowledged."""
    for entry in outbox.pending():
        await ws.send(json.dumps(make(MSG_FEEDBACK, entry=entry)))
    awaiting = sent.awaiting_ids()[:MAX_ASK]
    if awaiting:
        await ws.send(json.dumps(make(MSG_FEEDBACK_ASK, ids=awaiting)))


def on_message(msg: dict) -> None:
    """Client: an ack or a completed answer from the issuer."""
    if msg.get("type") == MSG_FEEDBACK_DONE:
        ids = msg.get("ids")
        sent.mark_completed(ids if isinstance(ids, list) else [])
    else:
        on_ack(msg)


def on_ack(msg: dict) -> None:
    """Client: the issuer has it, stop re-sending."""
    outbox.remove(str(msg.get("id") or ""))


async def handle(ws, token_meta: dict, hostname: str, ip: str, msg: dict) -> None:
    """Server: answer an ask, or receive an entry."""
    if msg.get("type") == MSG_FEEDBACK_ASK:
        ids = msg.get("ids")
        asked = [str(i) for i in ids[:MAX_ASK]] if isinstance(ids, list) else []
        await ws.send(json.dumps(make(MSG_FEEDBACK_DONE, ids=store.completed_ids(asked))))
        return
    await _receive(ws, token_meta, hostname, ip, msg)


async def _receive(ws, token_meta: dict, hostname: str, ip: str, msg: dict) -> None:
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
