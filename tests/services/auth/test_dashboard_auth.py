"""First-run password setup for the real dashboard login (review 2026-08-11, C2).

Today real mode has no way to set the dashboard password at all:
`set_password` exists but nothing calls it, the debug/debug seed is
(correctly) disabled with debug off, so `verify()` rejects every credential —
a fresh real install is locked out of its own dashboard by the very gate that
was added to protect it.

Desired contract, written RED first:

- `needs_setup()` is True exactly when debug is off and no password is stored.
- `create_initial_password()` establishes the admin password on a fresh
  install and returns True; login then succeeds with it.
- `create_initial_password()` REFUSES to overwrite an existing password and
  returns False — the unauthenticated first-run surface must never become a
  password-reset hole.

tests/core/test_dashboard_auth.py (legacy dir, closed) pins verify() and the
debug seed; this file is the mirrored home for the module and covers the
setup flow. Merge the two here when the legacy file is next touched.

No test in this file can reach a broker or the network: the module under test
is stdlib hashing over a temp file.
"""
from __future__ import annotations

import pytest

from backend.src import config as _config
from backend.src.services.auth import dashboard_auth as auth


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Same isolation as the legacy file: never touch the real hash file."""
    monkeypatch.setattr(auth, "_HASH_FILE", tmp_path / "dashboard_password.hash")
    yield


def _set_debug(monkeypatch, on: bool):
    monkeypatch.setattr(_config, "is_debug", lambda: on)


def test_fresh_real_install_reports_setup_needed(monkeypatch):
    _set_debug(monkeypatch, False)
    assert auth.needs_setup() is True


def test_no_setup_needed_in_debug_mode(monkeypatch):
    """Control: the debug/debug seed already covers debug-mode login — the
    first-run flow must not appear there."""
    _set_debug(monkeypatch, True)
    assert auth.needs_setup() is False


def test_no_setup_needed_once_a_password_exists(monkeypatch):
    _set_debug(monkeypatch, False)
    auth.set_password("s3cret-pass")
    assert auth.needs_setup() is False


def test_create_initial_password_enables_real_login(monkeypatch):
    _set_debug(monkeypatch, False)
    created = auth.create_initial_password("s3cret-pass")
    assert created is True
    assert auth.is_set() is True                       # persisted, not just returned
    assert auth.verify("admin", "s3cret-pass") is True


def test_create_initial_password_refuses_to_overwrite(monkeypatch):
    """The security property that makes an open first-run page safe: once a
    password exists, the unauthenticated path can never replace it."""
    _set_debug(monkeypatch, False)
    auth.set_password("original-pass")
    replaced = auth.create_initial_password("attacker-pass")
    assert replaced is False
    assert auth.verify("admin", "original-pass") is True
    assert auth.verify("admin", "attacker-pass") is False
