"""Remote admin/update-server API for the frontend's update panel.

Only the customer-install side is exposed here. The admin server
(`services/cluster/remote/server.py`) issues licence tokens and pushes update
archives; it is started by the composition root in `backend/src/app.py`, never
by a page, so it has no controller surface at all.

`app_version()` wraps `_app_version` on purpose: the page was importing a
private name across the layer boundary, which made a rename inside the client
a UI break.
"""
from __future__ import annotations

from backend.src.services.cluster.remote import client as _client
from backend.src.services.cluster.remote import tls as _tls

__all__ = [
    "SERVER_HOST", "SERVER_PORT",
    "get_or_create_token", "get_status", "get_stored_email",
    "request_registration", "app_version",
]

SERVER_HOST = _tls.SERVER_HOST
SERVER_PORT = _tls.SERVER_PORT


def get_or_create_token() -> str:
    return _client.get_or_create_token()


def get_status() -> dict:
    return _client.get_status()


def get_stored_email() -> str:
    return _client.get_stored_email()


def request_registration(email: str, nickname: str = "") -> None:
    return _client.request_registration(email, nickname)


def app_version() -> str:
    return _client._app_version()
