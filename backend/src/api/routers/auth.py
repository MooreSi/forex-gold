"""Login, logout and "who am I".

The session is a signed cookie. These routes are open — they are in
`auth.OPEN_PREFIXES` — because the login form cannot ask for a password if the
gate blocks the endpoint that checks it.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

from backend.src.api import auth as gate

router = APIRouter(prefix="/api/auth", tags=["auth"])


class Credentials(BaseModel):
    username: str = ""
    password: str = ""


class NewPassword(BaseModel):
    password: str


@router.get("/session")
async def session_state(request: Request) -> dict:
    """What the login page needs to decide what to render.

    `needs_setup` is why this is not just a boolean: a fresh real install has
    no password and no debug seed, so a plain login form could never succeed
    (review 2026-08-11, C2). The client offers one-time setup instead.

    `auto_login` is what `App.tsx` reads to skip the login page, so a build
    with no authentication must report it true whatever the operator's own
    setting says -- otherwise the gate is off on the server and the client
    still draws a password form with nothing behind it to check the answer.
    The operator's setting is still what decides it in a build that DOES
    authenticate: this only collapses the question when there is no login.

    `needs_setup` is reported unchanged either way. It is the answer to "does
    this install have a password yet", which stays true and stays useful --
    the Access tab still offers to set one -- and the client never reaches the
    setup form anyway once `auto_login` is true.
    """
    return {
        "authenticated": bool(request.session.get(gate.SESSION_KEY, False)),
        "needs_setup": gate.needs_setup(),
        "auto_login": True if not gate.authentication_required() else gate.auto_login_enabled(),
        "debug": gate.is_debug(),
    }


@router.post("/login")
async def login(request: Request, body: Credentials) -> dict:
    if not gate.verify(body.username, body.password):
        # Deliberately the same message for a wrong user and a wrong password.
        return {"ok": False, "message": "Incorrect username or password"}
    request.session[gate.SESSION_KEY] = True
    request.session["username"] = body.username
    return {"ok": True, "next": request.session.pop(gate.REFERRER_KEY, "/")}


@router.post("/setup")
async def setup(request: Request, body: NewPassword) -> dict:
    """One-time first-run password creation. `create_initial_password` refuses
    if a password already exists, so this cannot be used to reset one."""
    if not gate.create_initial_password(body.password):
        return {"ok": False, "message": "A password is already set."}
    request.session[gate.SESSION_KEY] = True
    return {"ok": True, "next": "/"}


@router.post("/logout")
async def logout(request: Request) -> dict:
    request.session.clear()
    return {"ok": True}
