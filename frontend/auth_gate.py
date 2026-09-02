"""Dashboard login gate — redirects unauthenticated requests to /login.

Wired from frontend/app.py. Requires `storage_secret` in ui.run (set in run.py)
so app.storage.user (a signed session cookie) works.
"""
from __future__ import annotations

from nicegui import app, ui
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import RedirectResponse

from backend.src.controllers import auth_controller as _auth
from backend.src.controllers import settings_controller as _cfg

# Owner's choice, 2026-09-02: log in with a password on every restart, or come
# straight in. DEFAULT FALSE -- an install that has never touched the setting
# keeps asking. This app places live orders with real money and this gate is
# the only thing between someone at the keyboard and the trading controls, so
# turning it off has to be a decision somebody made deliberately.
SETTING_KEY = "auto_login_enabled"


def _auto_login_enabled() -> bool:
    """Cheap enough for a per-request check: config.get serves from an
    in-memory dict after the first load."""
    try:
        return bool(_cfg.get_config(SETTING_KEY, False))
    except Exception:
        return False          # unreadable config must not open the door


def _may_pass(path: str) -> bool:
    """Whether an unauthenticated request may proceed.

    Split out of dispatch() so the decision can be tested without NiceGUI's
    request context.
    """
    if app.storage.user.get("authenticated", False):
        return True
    if any(path.startswith(p) for p in _OPEN_PREFIXES):
        return True
    return _auto_login_enabled()

# Paths reachable without a session. Everything under /_nicegui (framework
# internals: websocket, JS libraries) and /static (assets) must stay open or the
# login page itself can't render.
_OPEN_PREFIXES = ("/login", "/_nicegui", "/static", "/favicon")


class _AuthGate(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if _may_pass(path):
            return await call_next(request)
        # Remember where they were headed so we can return them after login.
        app.storage.user["referrer_path"] = path
        return RedirectResponse("/login")


def install() -> None:
    """Register the gate middleware and the /login + /logout pages. Idempotent."""
    if getattr(install, "_done", False):
        return
    install._done = True
    app.add_middleware(_AuthGate)

    @ui.page("/login")
    def _login_page():
        ui.dark_mode(True)
        if app.storage.user.get("authenticated", False):
            ui.navigate.to("/")
            return
        if _auth.needs_setup():
            # Fresh real install: no password exists and the debug seed is
            # off, so a plain login form could never succeed (review
            # 2026-08-11, C2). Offer the one-time setup instead.
            _first_run_setup()
            return

        def _attempt():
            if _auth.verify(username.value or "", password.value or ""):
                app.storage.user.update({"authenticated": True, "username": username.value})
                ui.navigate.to(app.storage.user.get("referrer_path", "/"))
            else:
                ui.notify("Incorrect username or password", color="negative")

        with ui.column().classes("absolute-center items-center gap-4"):
            ui.icon("lock", size="3rem").classes("text-teal-400")
            ui.label("FOREX Trader").classes("text-2xl font-bold")
            with ui.card().classes("w-80 p-6 gap-3"):
                username = ui.input("Username").props("outlined dense").classes("w-full")
                password = (ui.input("Password", password=True, password_toggle_button=True)
                            .props("outlined dense").classes("w-full")
                            .on("keydown.enter", _attempt))
                ui.button("Log in", on_click=_attempt).props("color=teal").classes("w-full")
                if _auth.is_debug() and not _auth.is_set():
                    ui.label("Debug mode — use debug / debug").classes("text-xs text-amber-400")

    def _first_run_setup():
        def _create():
            if not password.value or password.value != confirm.value:
                ui.notify("Enter the same non-empty password twice", color="negative")
                return
            if _auth.create_initial_password(password.value):
                app.storage.user.update({"authenticated": True, "username": "admin"})
                ui.navigate.to("/")
            else:
                # A password appeared since the page rendered (second tab /
                # second machine) — never overwrite; go log in with it.
                ui.notify("A password already exists — log in instead", color="negative")
                ui.navigate.to("/login")

        with ui.column().classes("absolute-center items-center gap-4"):
            ui.icon("lock_open", size="3rem").classes("text-teal-400")
            ui.label("FOREX Trader — first run").classes("text-2xl font-bold")
            with ui.card().classes("w-80 p-6 gap-3"):
                ui.label("Create the dashboard password. You will log in as "
                         "username admin from now on.").classes("text-sm")
                password = (ui.input("New password", password=True, password_toggle_button=True)
                            .props("outlined dense").classes("w-full"))
                confirm = (ui.input("Confirm password", password=True)
                           .props("outlined dense").classes("w-full")
                           .on("keydown.enter", _create))
                ui.button("Create password", on_click=_create).props("color=teal").classes("w-full")

    @ui.page("/logout")
    def _logout_page():
        app.storage.user.update({"authenticated": False})
        ui.navigate.to("/login")
