"""The remote-admin client's default, and the warning that goes with it.

Was "stays OFF by default" (security review 2026-08-08, C2: the client found a
server by LAN beacon and applied pushed code with no signature check and no TLS
verification).

**The contract changed on 2026-08-26 by the owner's decision** — Q001 #5,
amended, in docs/simon-handover/001-trading-defaults.md. Two things moved under
the original reasoning:

* This checkout stopped being an isolated fork. It was promoted to be the only
  app, and Simon uses the admin console for licence permissions and to see which
  clients are online — so a client that never connects is a broken feature, not
  a safe default.
* Upstream 0815cc6 deleted the zip-streaming push outright. An admin "update"
  now only asks the client to run its own git pull, so "applies pushed code" is
  no longer true.

What did NOT change: the TLS link runs CERT_NONE with no certificate pinning, so
the channel is still impersonable. These tests pin the new default AND pin the
warning to the risk that actually remains — a warning naming a deleted risk is
worse than none, because it teaches people to skip warnings.
"""
from __future__ import annotations

import logging

import backend.src.app as app
import backend.src.config as cfg_module


def test_config_default_now_enables_the_remote_client(monkeypatch):
    """Absent key means ON, so an existing install joins the fleet on upgrade."""
    monkeypatch.delenv("REMOTE_ADMIN_CLIENT_ENABLED", raising=False)
    monkeypatch.setattr(cfg_module, "_load_yaml", lambda: {})
    assert cfg_module.load()["remote_admin_client_enabled"] is True


def test_the_gate_is_still_a_real_gate():
    """Negative control: opting out must genuinely keep it off."""
    assert app._remote_client_enabled({"remote_admin_client_enabled": False}) is False
    assert app._remote_client_enabled({}) is True


def test_starting_warns_about_the_risk_that_actually_remains(caplog):
    with caplog.at_level(logging.WARNING):
        app._remote_client_enabled({"remote_admin_client_enabled": True})
    msg = " ".join(r.getMessage().lower() for r in caplog.records)
    assert "certificate" in msg, "the warning must name the unpinned certificate"
    assert "impersonate" in msg


def test_the_warning_no_longer_claims_pushed_code(caplog):
    """The specific regression this guards: upstream deleted that path, and a
    warning describing it would be false."""
    with caplog.at_level(logging.WARNING):
        app._remote_client_enabled({"remote_admin_client_enabled": True})
    msg = " ".join(r.getMessage().lower() for r in caplog.records)
    assert "apply pushed code" not in msg
    assert "will apply pushed code" not in msg


def test_opting_out_logs_nothing(caplog):
    with caplog.at_level(logging.WARNING):
        app._remote_client_enabled({"remote_admin_client_enabled": False})
    assert not caplog.records
