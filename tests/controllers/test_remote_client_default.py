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

**The contract changed a second time on 2026-09-02**, when bugs/014 closed the
licence/admin channel. CERT_NONE with nothing pinning it is no longer what this
link does: the internet path (`SERVER_HOST`) verifies against the CA bundled in
the build, and the LAN path trust-on-first-use pins the peer BEFORE the licence
token is sent. So the warning this file used to pin -- "certificate
verification DISABLED and no certificate pinning" -- became the very thing its
own docstring in `app.py` argues against, a warning naming a risk that no
longer exists.

These tests pin the new default AND pin the warning to the risk that actually
remains, which is trust-on-first-use's first connection on the LAN path. A
warning naming a deleted risk is worse than none, because it teaches people to
skip warnings.
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
    """What remains after bugs/014 is trust-on-first-use, not an absent check.

    On the LAN path the FIRST connection to a host has nothing to compare the
    peer against, so it pins whatever answers. Every later one must match. That
    is the exposure the operator needs told, and it is the only one left.
    """
    with caplog.at_level(logging.WARNING):
        app._remote_client_enabled({"remote_admin_client_enabled": True})
    msg = " ".join(r.getMessage().lower() for r in caplog.records)
    assert "first" in msg, "the warning must say WHICH connection is exposed"
    assert "impersonate" in msg


def test_the_warning_no_longer_claims_nothing_verifies_the_peer(caplog):
    """The regression this guards, and the reason this file changed.

    bugs/014 turned verification on. A warning still announcing it is off would
    send an operator to fix something already fixed, and -- worse -- teach them
    that this app's warnings are stale. Asserted as absences because that is
    the failure: text that survived the fix it describes.
    """
    with caplog.at_level(logging.WARNING):
        app._remote_client_enabled({"remote_admin_client_enabled": True})
    msg = " ".join(r.getMessage().lower() for r in caplog.records)
    assert "verification disabled" not in msg
    assert "no certificate pinning" not in msg
    assert "pinning is the tracked fix" not in msg, (
        "pinning is not tracked work any more -- it shipped 2026-09-02"
    )


def test_the_warning_says_the_internet_path_is_authenticated(caplog):
    """Not just what is wrong -- what is right, so the two paths are not

    conflated. `SERVER_HOST` is CA-verified by the handshake itself and has no
    first-connect window at all; only the LAN path does.
    """
    with caplog.at_level(logging.WARNING):
        app._remote_client_enabled({"remote_admin_client_enabled": True})
    msg = " ".join(r.getMessage().lower() for r in caplog.records)
    assert "lan" in msg, "the exposure must be scoped to the path that has it"


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
