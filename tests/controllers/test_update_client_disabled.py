"""The unauthenticated remote-admin/update client stays OFF by default.

Security review 2026-08-08, finding C2: the remote client discovers a server by
LAN beacon and applies pushed code with no signature check and no TLS
verification. On a normal single-node install it must never start, and its
default must never silently flip on (golden rule 3). Enabling it is an explicit,
loudly-warned opt-in.
"""
from __future__ import annotations

import logging

import backend.src.app as app
import backend.src.config as cfg_module


def test_config_default_keeps_remote_client_disabled(monkeypatch):
    """An upgrade must never flip this on. Pin the absent-key default to False."""
    monkeypatch.delenv("REMOTE_ADMIN_CLIENT_ENABLED", raising=False)
    monkeypatch.setattr(cfg_module, "_load_yaml", lambda: {})
    assert cfg_module.load()["remote_admin_client_enabled"] is False


def test_remote_client_not_started_by_default():
    assert app._remote_client_enabled({}) is False
    assert app._remote_client_enabled({"remote_admin_client_enabled": False}) is False


def test_explicit_enable_returns_true(caplog):
    """Negative control: the gate is a real gate, not hardwired off."""
    with caplog.at_level(logging.WARNING):
        assert app._remote_client_enabled({"remote_admin_client_enabled": True}) is True


def test_enabling_logs_an_unauthenticated_warning(caplog):
    with caplog.at_level(logging.WARNING):
        app._remote_client_enabled({"remote_admin_client_enabled": True})
    msg = " ".join(r.getMessage().lower() for r in caplog.records)
    assert "unauthenticated" in msg or "pushed code" in msg


def test_default_path_logs_no_warning(caplog):
    with caplog.at_level(logging.WARNING):
        app._remote_client_enabled({})
    assert not caplog.records
