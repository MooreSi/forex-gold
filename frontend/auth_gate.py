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

# Paths reachable without a session. Everything under /_nicegui (framework
# internals: websocket, JS libraries) and /static (assets) must stay open or the
# login page itself can't render.
_OPEN_PREFIXES = ("/login", "/_nicegui", "/static", "/favicon")


class _AuthGate(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if app.storage.user.get("authenticated", False) or any(path.startswith(p) for p in _OPEN_PREFIXES):
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

    @ui.page("/logout")
    def _logout_page():
        app.storage.user.update({"authenticated": False})
        ui.navigate.to("/login")
