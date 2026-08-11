"""The login gate must offer first-run password setup in real mode
(review 2026-08-11, C2).

`auth_gate.py`'s pages are closures over NiceGUI request context, so until a
render harness exists the *wiring* is pinned at source level — the same
technique the structural gates use. The page module must branch on the
controller's `needs_setup()` and call `create_initial_password(...)`;
without that caller, a fresh real install shows a login form that can never
succeed (verify() rejects everything when no password is stored and debug is
off).

The behaviour itself (needs_setup / create_initial_password semantics) is
tested where it lives: tests/services/auth/test_dashboard_auth.py and
tests/controllers/test_auth_controller.py. This file only proves the frontend
actually uses it — the 2026-08-11 security review found `set_password` had no
caller anywhere, which is precisely how a "done" login feature shipped as a
lockout.

No test in this file can reach a broker or the network: it reads source text
and module constants only.
"""
from __future__ import annotations

import re
from pathlib import Path

from frontend import auth_gate

_SOURCE = Path(auth_gate.__file__).read_text(encoding="utf-8")
_CALL = re.compile(r"_auth\.(needs_setup|create_initial_password)\s*\(")


def test_login_surface_wires_the_first_run_setup_flow():
    called = set(_CALL.findall(_SOURCE))
    assert called == {"needs_setup", "create_initial_password"}, (
        "auth_gate must branch on _auth.needs_setup() and call "
        "_auth.create_initial_password(...) — a login page with no set-password "
        f"path locks a fresh real install out. Found calls: {sorted(called)}"
    )


def test_the_wiring_scan_can_actually_see_a_call():
    """Negative control: a presence assertion is worthless if the pattern is
    blind."""
    assert _CALL.search("if _auth.needs_setup():")
    assert _CALL.search("_auth.create_initial_password(pw.value)")
    assert not _CALL.search("_auth.verify(u, p)")


def test_login_stays_reachable_without_a_session():
    """Control (green today): the gate must keep its own login surface open,
    or no one can ever authenticate."""
    assert any("/login".startswith(p) for p in auth_gate._OPEN_PREFIXES)
