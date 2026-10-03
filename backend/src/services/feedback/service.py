"""Submitting feedback, and receiving it on the issuer.

`submit` runs on any install: it validates, then either records the entry
directly (the issuer's own machine) or queues it for the remote connection to
deliver. `receive` runs on the issuer: store first, announce only if new.
"""
from __future__ import annotations

import asyncio
import time
import uuid

from backend.src.services.feedback import notify, outbox, store

KINDS = ("feature", "bug", "feedback")
MAX_MESSAGE = 4000
_TEXT_FIELDS = ("name", "email", "hostname", "version")
_background: set = set()


def clean(raw: dict) -> dict:
    """A trusted-shape entry from untrusted input, or ValueError."""
    kind = raw.get("kind")
    message = str(raw.get("message") or "").strip()[:MAX_MESSAGE]
    entry_id = str(raw.get("id") or "")
    if kind not in KINDS:
        raise ValueError("Choose feature request, bug or general feedback.")
    if not message:
        raise ValueError("Write something first.")
    if not entry_id:
        raise ValueError("Missing id.")
    out = {"id": entry_id[:64], "kind": kind, "message": message,
           "submitted_at": float(raw.get("submitted_at") or time.time())}
    out.update({k: str(raw.get(k) or "")[:200] for k in _TEXT_FIELDS})
    return out


def _identity() -> dict:
    """Who is sending, as far as this install knows. Never raises."""
    try:
        import socket
        from backend.src.services.cluster.remote import client
        return {"name": client.get_stored_nickname(), "email": client.get_stored_email(),
                "hostname": socket.gethostname(), "version": client._app_version()}
    except Exception:                             # noqa: BLE001
        return {}


def _is_issuer() -> bool:
    from backend.src.config.licence.issuer import is_licence_issuer_machine
    return is_licence_issuer_machine()


def submit(kind: str, message: str, *, issuer: bool | None = None) -> dict:
    """Validate and hand off. Raises ValueError for input the user can fix."""
    entry = clean({"id": uuid.uuid4().hex, "kind": kind, "message": message,
                   **_identity()})
    if _is_issuer() if issuer is None else issuer:
        task = asyncio.ensure_future(receive(entry))
        _background.add(task)
        task.add_done_callback(_background.discard)
    else:
        outbox.add(entry)
    return entry


async def receive(entry: dict) -> bool:
    """Record `entry` and tell the owner. False when it was already held."""
    if not store.add(entry):
        return False
    await notify.announce(entry)
    return True
