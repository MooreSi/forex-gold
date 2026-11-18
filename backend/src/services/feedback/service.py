"""Submitting feedback, and receiving it on the issuer.

`submit` runs on any install: it validates, then either records the entry
directly (the issuer's own machine) or queues it for the remote connection to
deliver. `receive` runs on the issuer: store first, announce only if new.

Either way the id goes on this install's sent list, so the sender can be told
when the owner marks it Completed (`completed_notices`).
"""
from __future__ import annotations

import asyncio
import time
import logging
import re
import uuid

from backend.src.services.feedback import notify, outbox, sent, store

log = logging.getLogger(__name__)

KINDS = ("feature", "bug", "feedback")
MAX_MESSAGE = 4000
_TEXT_FIELDS = ("name", "email", "hostname", "version")
#: Shape only: something@something.tld, no spaces. Whether it is deliverable
#: is the owner's reply's problem; this catches the typo before it queues.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MAX_EMAIL = 200
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


def reply_email(raw: str) -> str:
    """The sender's optional reply address, trimmed. "" when blank, or
    ValueError when it is not shaped like an address."""
    email = str(raw or "").strip()
    if email and (len(email) > MAX_EMAIL or not _EMAIL.match(email)):
        raise ValueError("That email address does not look right.")
    return email


def submit(kind: str, message: str, *, email: str = "",
           issuer: bool | None = None) -> dict:
    """Validate and hand off. Raises ValueError for input the user can fix.

    `email` is the address the sender typed so the owner can reply. When
    given it replaces the install's stored email on the entry, so every place
    the owner reads feedback shows the one they asked to be answered at.
    """
    reply = reply_email(email)
    identity = _identity()
    if reply:
        identity["email"] = reply
    entry = clean({"id": uuid.uuid4().hex, "kind": kind, "message": message,
                   **identity})
    sent.add(entry)
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


def completed_notices(*, issuer: bool | None = None) -> list[dict]:
    """This install's feedback the owner has marked Completed and the sender
    has not yet seen. Read on every header poll, so it never raises.

    The issuer's own feedback never crosses the wire, so on the issuer the
    awaiting ids are checked against its own store here instead.
    """
    try:
        if _is_issuer() if issuer is None else issuer:
            sent.mark_completed(store.completed_ids(sent.awaiting_ids()))
        return [{"id": r["id"], "kind": r.get("kind", "feedback"),
                 "message": r.get("message", ""),
                 "completed_at": r.get("completed_at", 0)}
                for r in sent.notices()]
    except Exception as exc:                      # noqa: BLE001
        log.warning("[Feedback] Could not read completed notices: %s", exc)
        return []


def mark_seen(entry_id: str) -> bool:
    """The sender closed the popup. False when there is no such notice."""
    return sent.mark_seen(entry_id)


_INBOX_FIELDS = ("id", "kind", "message", "name", "email", "hostname",
                 "version", "submitted_at")


def unread_count(*, issuer: bool | None = None) -> int:
    """New feedback for the admin node's title-bar alert. 0 on any other
    install. Read on every header poll, so it never raises."""
    try:
        if not (_is_issuer() if issuer is None else issuer):
            return 0
        return len(store.unread())
    except Exception as exc:                      # noqa: BLE001
        log.warning("[Feedback] Could not count unread feedback: %s", exc)
        return 0


def unread_feedback(*, issuer: bool | None = None) -> list[dict]:
    """The entries behind the alert, newest first. Empty off the issuer."""
    if not (_is_issuer() if issuer is None else issuer):
        return []
    return [{k: r.get(k) for k in _INBOX_FIELDS} for r in store.unread()]


def mark_read(ids, *, issuer: bool | None = None) -> int:
    """The owner opened the alert. Only the ids shown, so feedback that
    arrived meanwhile keeps the alert lit. Does not complete anything."""
    if not (_is_issuer() if issuer is None else issuer):
        return 0
    return store.mark_read(ids)
