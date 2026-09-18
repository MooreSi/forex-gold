"""The dashboard login gate, ported from `frontend/auth_gate.py`.

Same three behaviours, same default, different transport. What changed and
what deliberately did not:

* **Unchanged.** `auto_login_enabled` defaults to False, so an install that has
  never touched the setting keeps asking. This app places live orders and this
  gate is the only thing between someone at the keyboard and the trading
  controls, so turning it off stays a decision somebody made deliberately.
* **Unchanged.** Unreadable config must not open the door. The bare `except`
  returning False in `_auto_login_enabled` is load-bearing, not sloppiness.
* **Unchanged.** A fresh install with no password and no debug seed is offered
  one-time setup rather than a login form that could never succeed
  (review 2026-08-11, C2).
* **Changed.** An unauthenticated API request gets **401 JSON**, never a
  redirect. A 302 to an HTML login page inside an XHR is how a session expiry
  becomes a login form rendered inside the trading panel. Only a browser
  navigation to a page route redirects.
* **Gone.** `/_nicegui` is no longer an open prefix; there is no NiceGUI.

The session is a signed cookie over the same per-install secret
`run.py:_dashboard_storage_secret()` already writes, so an existing install
keeps its secret file.
"""
from __future__ import annotations

import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from backend.src.controllers import auth_controller as _auth
from backend.src.controllers import settings_controller as _cfg

log = logging.getLogger(__name__)

# Owner's choice, 2026-09-02. DEFAULT FALSE — see the module docstring.
SETTING_KEY = "auto_login_enabled"

# Reachable without a session. The login route and everything the login page
# itself loads must stay open, or it cannot render to ask for the password.
OPEN_PREFIXES = ("/login", "/api/auth/", "/static", "/favicon", "/assets", "/healthz")

SESSION_KEY = "authenticated"
REFERRER_KEY = "referrer_path"


def auto_login_enabled() -> bool:
    """Cheap enough for a per-request check: `get_config` serves from an
    in-memory dict after the first load."""
    try:
        return bool(_cfg.get_config(SETTING_KEY, False))
    except Exception:
        return False          # unreadable config must not open the door


def may_pass(session: dict, path: str) -> bool:
    """Whether an unauthenticated request may proceed.

    Split out of dispatch so the decision is testable without a request
    context — exactly as `frontend/auth_gate._may_pass` was.
    """
    if session.get(SESSION_KEY, False):
        return True
    if any(path.startswith(p) for p in OPEN_PREFIXES):
        return True
    return auto_login_enabled()


def _is_api(path: str) -> bool:
    return path.startswith("/api/")


class AuthGate(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        session = request.session
        if may_pass(session, path):
            return await call_next(request)
        if _is_api(path):
            return JSONResponse(
                status_code=401,
                content={"error": {"kind": "unauthenticated",
                                   "message": "Sign in to continue.",
                                   "ref": None}},
            )
        # A browser navigation. Remember where they were headed.
        session[REFERRER_KEY] = path
        return RedirectResponse("/login")


def needs_setup() -> bool:
    return bool(_auth.needs_setup())


def verify(username: str, password: str) -> bool:
    return bool(_auth.verify(username, password))


def create_initial_password(password: str) -> bool:
    return bool(_auth.create_initial_password(password))


def is_debug() -> bool:
    return bool(_auth.is_debug())
