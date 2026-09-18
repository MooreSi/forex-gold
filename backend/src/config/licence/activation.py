"""The activation screen's decisions, separated from how they are rendered.

The screen itself used to be ~280 lines of NiceGUI. Every decision it made was
tangled with a widget, so none of it could be tested and the whole thing had to
be re-read to answer "what does it do when the admin server is down?".

This module is those decisions and nothing else: validate what was typed,
verify a key against THIS machine, describe what the remote client is actually
doing. It imports no web framework, touches no global state, and every function
here is called by `activation_server.py` and by tests.

**It grants nothing.** `verify_key` calls the same Ed25519 check `enforce()`
uses, and a key that does not verify is refused here exactly as it is there.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

MIN_NICKNAME = 2


def validate_details(nickname: str, email: str) -> Optional[str]:
    """The complaint to show, or None when the form is usable.

    Deliberately the same rules for the automated and the manual path: a
    licence issued against a name the operator did not actually type is a
    support conversation nobody can win.
    """
    if len(nickname.strip()) < MIN_NICKNAME:
        return f"Enter a name or nickname (at least {MIN_NICKNAME} characters)."
    email = email.strip()
    if not email or "@" not in email:
        return "Enter a valid email address."
    return None


def parse_activation_code(code: str) -> tuple[str, str, str]:
    """KEY, KEY|EXPIRY or KEY|EXPIRY|TYPE -> (key, expiry, type).

    Moved verbatim from `guard._parse_activation_code` so the two cannot drift;
    the guard now calls this one.
    """
    parts = [p.strip() for p in code.strip().split("|")]
    key = parts[0]
    expiry = parts[1] if len(parts) >= 2 else "perpetual"
    ltype = parts[2] if len(parts) >= 3 else (
        "Perpetual" if expiry == "perpetual" else "Fixed Term")
    return key, expiry, ltype


def verify_licence_key(machine_id: str, expiry_date: str, key: str) -> bool:
    """The real signature check, under the real name.

    Named identically to the function it delegates to, deliberately: the
    unverified-save scanner in `tests/licence/test_no_unverified_save.py`
    recognises a guard by NAME, and a local alias would have made this save
    site look unguarded — or worse, would have let a future one be unguarded
    without the scanner noticing.
    """
    from backend.src.config.licence.verify import verify_licence_key as _verify

    return bool(_verify(machine_id, expiry_date, key))


def activate_manually(machine_id: str, nickname: str, email: str,
                      raw_code: str) -> dict:
    """Verify a pasted key and store it. Returns what the screen should say.

    `{"ok": False, "message": ...}` for every refusal, `{"ok": True}` once the
    licence is stored. Nothing is stored unless the signature verifies — that
    is the whole point of this function existing separately from the form.
    """
    complaint = validate_details(nickname, email)
    if complaint:
        return {"ok": False, "message": complaint}
    if not raw_code.strip():
        return {"ok": False, "message": "Enter your licence key."}

    key, expiry_date, licence_type = parse_activation_code(raw_code)
    if not verify_licence_key(machine_id, expiry_date, key):
        return {"ok": False, "message":
                "Invalid licence key — this key was not issued for this machine."}

    from backend.src.config.licence import store as _store

    _store.save({
        "machine_id": machine_id,
        "nickname": nickname.strip(),
        "email": email.strip(),
        "expiry_date": expiry_date,
        "licence_type": licence_type,
        "licence_key": key,
    })
    return {"ok": True, "message": "Activated. Restart the app to continue."}


def describe_delivery(status: dict) -> dict:
    """What the screen should say about the registration request.

    The wording matters and is the reason this is a function. On 2026-08-07 a
    user was told their request was "awaiting approval" while the admin server
    was down — so nothing had been transmitted, nothing ever arrived, and the
    screen gave no way to tell. `request_registration()` only QUEUES a
    connection; the request itself is sent later and only if the client reaches
    the server. So this reports what actually happened, not what was asked for.
    """
    if status.get("registration_sent_at"):
        return {
            "state": "delivered",
            "message": ("Request delivered — awaiting administrator approval. "
                        "The app will activate automatically once approved."),
        }
    error = str(status.get("last_error") or "")
    if error and error != "contacting administrator server":
        return {
            "state": "retrying",
            "message": ("Cannot reach the administrator server yet — still "
                        f"retrying. ({error}) Leave this screen open; the "
                        "request is sent automatically as soon as the server "
                        "answers."),
        }
    return {"state": "sending", "message": "Contacting administrator server..."}
