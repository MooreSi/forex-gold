"""The dashboard must bind to loopback by default.

The UI on :8888 can place and close live orders and has no login of its own
(security review 2026-08-08, finding C1: it bound to 0.0.0.0). These tests pin
the default to 127.0.0.1, prove an explicit override is still possible, and
prove the loopback check itself can distinguish a non-loopback host (the
negative control — a check that can't fail proves nothing).

They assert on resolved config, never by opening a real socket; the boot smoke
test already proves the server actually serves.
"""
from __future__ import annotations

import logging

import pytest

import run


def test_default_host_is_loopback(monkeypatch):
    """config.load() defaults the bind host to 127.0.0.1 with no yaml, no env."""
    import backend.src.config as cfg_module

    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.setattr(cfg_module, "_load_yaml", lambda: {})

    cfg = cfg_module.load()

    assert cfg["host"] == "127.0.0.1"


def test_host_override_requires_explicit_config(monkeypatch):
    """Only an explicit yaml key or HOST env widens the bind — never by accident."""
    import backend.src.config as cfg_module

    monkeypatch.delenv("HOST", raising=False)
    monkeypatch.setattr(cfg_module, "_load_yaml", lambda: {"host": "0.0.0.0"})
    assert cfg_module.load()["host"] == "0.0.0.0"

    monkeypatch.setenv("HOST", "192.168.1.5")
    monkeypatch.setattr(cfg_module, "_load_yaml", lambda: {})
    assert cfg_module.load()["host"] == "192.168.1.5"


def test_resolve_bind_host_returns_configured_host():
    assert run._resolve_bind_host({"host": "127.0.0.1"}) == "127.0.0.1"
    assert run._resolve_bind_host({"host": "0.0.0.0"}) == "0.0.0.0"


def test_resolve_bind_host_defaults_to_loopback():
    """A cfg missing the key still resolves to loopback, never 0.0.0.0."""
    assert run._resolve_bind_host({}) == "127.0.0.1"


def test_non_loopback_bind_warns(caplog):
    with caplog.at_level(logging.WARNING):
        run._resolve_bind_host({"host": "0.0.0.0"})
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert any("login" in r.getMessage().lower() or "loopback" in r.getMessage().lower()
               for r in caplog.records)


def test_loopback_bind_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING):
        run._resolve_bind_host({"host": "127.0.0.1"})
    assert not caplog.records


def test_loopback_detector_can_fail():
    """Negative control: the loopback check must classify 0.0.0.0 as NOT loopback.

    If this ever returns True for 0.0.0.0, every 'binds to loopback' assertion
    above is worthless.
    """
    assert run._is_loopback("127.0.0.1") is True
    assert run._is_loopback("localhost") is True
    assert run._is_loopback("0.0.0.0") is False
    assert run._is_loopback("192.168.1.5") is False
