"""Dashboard login service: real password verify + the debug-only seed.

The seed (debug/debug) must work ONLY in debug mode with no password set, and
must never weaken a real, configured password.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.src import config as _config
from backend.src.services.auth import dashboard_auth as auth


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(auth, "_HASH_FILE", tmp_path / "dashboard_password.hash")
    yield


def _set_debug(monkeypatch, on: bool):
    monkeypatch.setattr(_config, "is_debug", lambda: on)


def test_debug_seed_works_only_in_debug(monkeypatch):
    _set_debug(monkeypatch, True)
    assert auth.verify("debug", "debug") is True
    assert auth.verify("debug", "wrong") is False        # control: wrong pw rejected
    assert auth.verify("someone", "debug") is False       # control: wrong user rejected


def test_debug_seed_disabled_when_not_debug(monkeypatch):
    _set_debug(monkeypatch, False)
    assert auth.verify("debug", "debug") is False


def test_real_password_roundtrip(monkeypatch):
    _set_debug(monkeypatch, False)
    auth.set_password("s3cret-pass")
    assert auth.is_set() is True
    assert auth.verify("admin", "s3cret-pass") is True
    assert auth.verify("admin", "nope") is False          # control
    assert auth.verify("attacker", "s3cret-pass") is False # wrong username


def test_debug_seed_ignored_once_password_set(monkeypatch):
    _set_debug(monkeypatch, True)          # even in debug…
    auth.set_password("realpw")
    assert auth.verify("debug", "debug") is False  # …the seed no longer applies
    assert auth.verify("admin", "realpw") is True


def test_hash_file_stores_no_plaintext(monkeypatch):
    _set_debug(monkeypatch, False)
    auth.set_password("plaintext-secret-123")
    raw = Path(auth._HASH_FILE).read_bytes()
    assert b"plaintext-secret-123" not in raw
