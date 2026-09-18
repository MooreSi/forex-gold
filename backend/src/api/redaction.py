"""Keeping a credential out of a response. One copy, on purpose.

This was a private helper inside `routers/settings.py` until the connection
settings moved to `routers/notifications.py` on 2026-09-18. Two routers now
answer with stored configuration, and the one thing that must not be got wrong
twice is a password reaching a browser — which cannot be taken back once it
has. A second copy of this rule is a copy that will not be updated when a
service grows a new credential field.

A denylist rather than an allowlist, deliberately: the services grow fields,
and a new secret must be excluded by default rather than after somebody
remembers to add it.
"""
from __future__ import annotations

__all__ = ["SECRET_FIELDS", "redacted"]

SECRET_FIELDS = ("password", "api_key", "api_hash", "token", "secret", "passphrase")


def redacted(values: dict) -> dict:
    """Everything except the secrets, with a flag saying one is set.

    `"password": ""` and `"password": null` both read as "no password
    configured", which is why the flag is separate from the value.
    """
    out: dict = {}
    for key, value in (values or {}).items():
        if any(s in key.lower() for s in SECRET_FIELDS):
            out[f"{key}_set"] = bool(value)
        else:
            out[key] = value
    return out
