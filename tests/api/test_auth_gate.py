"""The login gate, ported from `frontend/auth_gate.py`.

Its NiceGUI twin is `tests/frontend/test_auth_gate.py` and
`test_auto_login_option.py`. Every behaviour those pin has a test here; that
correspondence is what makes deleting them legitimate when NiceGUI goes
(task 070), and the mapping is in that task's commit rather than left implicit.

`auto_login_enabled` reads the config file in the user data directory, so every
test that depends on it **sets it**. A test that reads the operator's real
config asserts something about whoever ran it: on this machine the setting
happens to be on, which silently turned the first version of the 401 test
green.
"""
from __future__ import annotations

import pytest

from backend.src.api import auth as gate


@pytest.fixture
def gated(make_client, monkeypatch):
    """A client with the gate on and auto-login explicitly off."""
    monkeypatch.setattr(gate, "auto_login_enabled", lambda: False)
    return make_client(auth=True)


# ── what the gate lets through ───────────────────────────────────────────────

def test_an_unauthenticated_api_request_is_401_json_not_a_redirect(gated):
    """A 302 to an HTML login page inside an XHR is how a session expiry turns
    into a login form rendered inside the trading panel."""
    r = gated.get("/api/trading/risk")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["error"]["kind"] == "unauthenticated"


def test_an_unauthenticated_page_request_redirects_to_login(gated):
    r = gated.get("/trading", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/login"


def test_the_login_endpoints_are_reachable_without_a_session(gated):
    """Negative control on the two tests above: if the gate blocked these, the
    login page could not ask for a password and the 401 would be permanent."""
    assert gated.get("/api/auth/session").status_code == 200


def test_the_open_prefixes_are_the_ones_the_login_page_needs():
    """Pinned as a list because the cost of a missing entry is a dashboard
    nobody can log into, and the cost of a spurious one is an open door."""
    assert gate.OPEN_PREFIXES == (
        "/login", "/api/auth/", "/static", "/favicon", "/assets", "/healthz",
    )
    assert "/_nicegui" not in gate.OPEN_PREFIXES


# ── may_pass, decided without a request context ──────────────────────────────

@pytest.mark.parametrize("path", ["/api/trading/risk", "/trading", "/"])
def test_no_session_and_no_auto_login_means_no(path, monkeypatch):
    monkeypatch.setattr(gate, "auto_login_enabled", lambda: False)
    assert gate.may_pass({}, path) is False


def test_a_session_passes_every_path(monkeypatch):
    monkeypatch.setattr(gate, "auto_login_enabled", lambda: False)
    assert gate.may_pass({gate.SESSION_KEY: True}, "/api/trading/orders/market") is True


def test_auto_login_on_opens_the_door(monkeypatch):
    monkeypatch.setattr(gate, "auto_login_enabled", lambda: True)
    assert gate.may_pass({}, "/api/trading/risk") is True


def test_unreadable_config_does_not_open_the_door(monkeypatch):
    """The bare except in `auto_login_enabled` is load-bearing. A config file
    that cannot be read is not consent to skip the password."""
    def boom(key, default=None):
        raise OSError("config.yaml is unreadable")

    monkeypatch.setattr(gate._cfg, "get_config", boom)
    assert gate.auto_login_enabled() is False
    assert gate.may_pass({}, "/api/trading/risk") is False


def test_the_default_for_an_install_that_never_set_it_is_off(monkeypatch):
    """Owner's choice 2026-09-02, and the reason it is not True: this gate is
    the only thing between someone at the keyboard and the trading controls."""
    seen = {}

    def absent(key, default=None):
        seen["default"] = default
        return default

    monkeypatch.setattr(gate._cfg, "get_config", absent)
    assert gate.auto_login_enabled() is False
    assert seen["default"] is False


# ── logging in ───────────────────────────────────────────────────────────────

def test_a_correct_password_sets_a_session_the_next_request_accepts(gated, monkeypatch):
    monkeypatch.setattr(gate, "verify", lambda u, p: True)
    r = gated.post("/api/auth/login", json={"username": "simon", "password": "right"})
    assert r.json()["ok"] is True
    assert gated.get("/api/trading/risk").status_code != 401


def test_a_wrong_password_sets_no_session(gated, monkeypatch):
    """Negative control. Without it, a login route that returned ok for
    everything would pass the test above."""
    monkeypatch.setattr(gate, "verify", lambda u, p: False)
    r = gated.post("/api/auth/login", json={"username": "simon", "password": "wrong"})
    assert r.json()["ok"] is False
    assert gated.get("/api/trading/risk").status_code == 401


def test_the_failure_message_does_not_say_which_half_was_wrong(gated, monkeypatch):
    monkeypatch.setattr(gate, "verify", lambda u, p: False)
    msg = gated.post("/api/auth/login", json={"username": "nobody", "password": "x"}).json()
    assert msg["message"] == "Incorrect username or password"


def test_logging_out_drops_the_session(gated, monkeypatch):
    monkeypatch.setattr(gate, "verify", lambda u, p: True)
    gated.post("/api/auth/login", json={"username": "simon", "password": "right"})
    gated.post("/api/auth/logout")
    assert gated.get("/api/trading/risk").status_code == 401


def test_login_returns_where_the_user_was_headed(gated, monkeypatch):
    """The gate records the path it turned away so the user lands where they
    meant to go, not on a default tab."""
    monkeypatch.setattr(gate, "verify", lambda u, p: True)
    gated.get("/history", follow_redirects=False)
    r = gated.post("/api/auth/login", json={"username": "simon", "password": "right"})
    assert r.json()["next"] == "/history"


# ── first run ────────────────────────────────────────────────────────────────

def test_a_fresh_install_is_offered_setup_rather_than_an_impossible_login(
    gated, monkeypatch,
):
    """Review 2026-08-11, C2: no password and no debug seed means a login form
    could never succeed, whatever the user types."""
    monkeypatch.setattr(gate, "needs_setup", lambda: True)
    assert gated.get("/api/auth/session").json()["needs_setup"] is True


def test_setup_refuses_when_a_password_already_exists(gated, monkeypatch):
    """Otherwise the setup route is a password reset with no authentication in
    front of it."""
    monkeypatch.setattr(gate, "create_initial_password", lambda p: False)
    r = gated.post("/api/auth/setup", json={"password": "hunter2"})
    assert r.json()["ok"] is False
    assert gated.get("/api/trading/risk").status_code == 401
