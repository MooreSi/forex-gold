"""Node pairing, autostart, restart and applying a release.

None of it trades; all of it can stop the app trading. The two assertions that
matter are about a secret and about a one-way action:

* **The sync token is never read back.** `GET /state` reports whether one
  exists; only the endpoint that CREATES one returns the plaintext, once,
  because that is the single moment it is supposed to be readable.
* **Generating a token overwrites the previous one**, so the response says so
  rather than letting the operator find out when the other node stops
  connecting.

Nothing here restarts anything: the runtime is a recorder.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import node as node_router


@pytest.fixture
def node(monkeypatch, sentinel_engine):
    state = {
        "token": "already-paired",
        "active_trader": "local",
        "registration": {"approved": True, "machine_id": "abc123"},
        "email": "simon@example.com",
        "autostart": {"supported": True, "installed": False, "armed": False},
        "update": {"version": "1.5.0"},
        "writes": [],
    }

    async def _check():
        return state["update"]

    async def _apply():
        state["writes"].append(("apply_update",))
        return {"ok": True}

    async def _restart(engine):
        state["writes"].append(("restart", engine))
        return "restarting"

    monkeypatch.setattr(node_router.system_ctl, "app_version", lambda: "1.4.2")
    monkeypatch.setattr(node_router.system_ctl, "check_for_update", _check)
    monkeypatch.setattr(node_router.system_ctl, "apply_update", _apply)
    monkeypatch.setattr(node_router.system_ctl, "summarise_changes",
                        lambda u: ["Ported the News tab"])
    monkeypatch.setattr(node_router.system_ctl, "autostart_is_supported",
                        lambda: state["autostart"]["supported"])
    monkeypatch.setattr(node_router.system_ctl, "autostart_is_installed",
                        lambda: state["autostart"]["installed"])
    monkeypatch.setattr(node_router.system_ctl, "autostart_is_armed",
                        lambda: state["autostart"]["armed"])
    monkeypatch.setattr(node_router.system_ctl, "autostart_enable",
                        lambda: state["writes"].append(("autostart", True)))
    monkeypatch.setattr(node_router.system_ctl, "autostart_disable",
                        lambda: state["writes"].append(("autostart", False)))
    monkeypatch.setattr(node_router.system_ctl, "AUTOSTART_CHECK_INTERVAL_SECS", 300)
    monkeypatch.setattr(node_router.settings_ctl, "get_active_trader",
                        lambda: state["active_trader"])
    monkeypatch.setattr(node_router.settings_ctl, "set_active_trader",
                        lambda v: state["writes"].append(("active_trader", v)))
    monkeypatch.setattr(node_router.node_ctl, "get_sync_token", lambda: state["token"])
    monkeypatch.setattr(node_router.node_ctl, "generate_sync_token",
                        lambda: state["writes"].append(("token",)) or "brand-new-token")
    monkeypatch.setattr(node_router.node_ctl, "restart_app", _restart)
    monkeypatch.setattr(node_router.remote_ctl, "get_status", lambda: state["registration"])
    monkeypatch.setattr(node_router.remote_ctl, "get_stored_email", lambda: state["email"])
    monkeypatch.setattr(node_router.remote_ctl, "request_registration",
                        lambda email, nickname: state["writes"].append(("register", email, nickname)))
    return state


# ── The secret ───────────────────────────────────────────────────────────────

def test_the_state_says_a_token_exists_without_returning_it(make_client, node):
    body = make_client().get("/api/node/state").json()

    assert body["sync_token_set"] is True
    assert "already-paired" not in make_client().get("/api/node/state").text


def test_an_unpaired_node_says_so(make_client, node):
    node["token"] = ""

    assert make_client().get("/api/node/state").json()["sync_token_set"] is False


def test_generating_a_token_returns_it_once_and_warns_that_it_replaced_one(
    make_client, node,
):
    body = make_client().post("/api/node/sync-token").json()

    assert body["token"] == "brand-new-token"
    assert "replaces any previous token" in body["note"]
    assert "not shown again" in body["note"]
    assert ("token",) in node["writes"]


def test_reading_the_state_never_generates_a_token(make_client, node):
    """Negative control. A poll that rotated the token would break the pairing
    every few seconds."""
    make_client().get("/api/node/state")

    assert node["writes"] == []


# ── Which node trades ────────────────────────────────────────────────────────

def test_the_active_trader_is_read_and_written(make_client, node):
    assert make_client().get("/api/node/state").json()["active_trader"] == "local"

    make_client().put("/api/node/active-trader", json={"trader": "remote_vps"})

    assert ("active_trader", "remote_vps") in node["writes"]


# ── Registration ─────────────────────────────────────────────────────────────

def test_registering_forwards_the_email_and_nickname(make_client, node):
    make_client().post("/api/node/register",
                       json={"email": " simon@example.com ", "nickname": " Mac "})

    assert ("register", "simon@example.com", "Mac") in node["writes"]


def test_registering_without_an_email_is_refused(make_client, node):
    r = make_client().post("/api/node/register", json={"email": "   "})

    assert r.status_code == 400
    assert node["writes"] == []


# ── Autostart ────────────────────────────────────────────────────────────────

def test_autostart_reports_whether_the_platform_supports_it(make_client, node):
    body = make_client().get("/api/node/state").json()["autostart"]

    assert body["supported"] is True
    assert body["installed"] is False
    assert body["check_interval_secs"] == 300


def test_enabling_autostart_on_an_unsupported_platform_is_refused(make_client, node):
    """Rather than reporting success for something that did not happen."""
    node["autostart"]["supported"] = False

    r = make_client().put("/api/node/autostart", json={"enabled": True})

    assert r.status_code == 409
    assert node["writes"] == []


def test_turning_autostart_off_forwards_false(make_client, node):
    make_client().put("/api/node/autostart", json={"enabled": False})

    assert ("autostart", False) in node["writes"]


# ── Updates ──────────────────────────────────────────────────────────────────

def test_the_update_check_reports_the_running_version_beside_the_new_one(
    make_client, node,
):
    body = make_client().get("/api/node/update").json()

    assert body["current"] == "1.4.2"
    assert body["update"]["version"] == "1.5.0"
    assert body["changes"] == ["Ported the News tab"]


def test_no_update_available_lists_no_changes(make_client, node):
    """Negative control: summarising `None` would render a changelog for a
    release that does not exist."""
    node["update"] = None

    body = make_client().get("/api/node/update").json()

    assert body["update"] is None
    assert body["changes"] == []


def test_checking_for_an_update_never_applies_one(make_client, node):
    make_client().get("/api/node/update")

    assert node["writes"] == []


def test_applying_an_update_is_a_post(make_client, node):
    assert make_client().get("/api/node/update/apply").status_code == 405

    make_client().post("/api/node/update/apply")

    assert ("apply_update",) in node["writes"]


# ── Restart ──────────────────────────────────────────────────────────────────

def test_restarting_goes_through_the_runtime(make_client, node, sentinel_engine):
    """It holds the bot offset that has to be persisted first; stopping the
    server without that loses it."""
    body = make_client().post("/api/node/restart").json()

    assert body["result"] == "restarting"
    assert ("restart", sentinel_engine) in node["writes"]


def test_restart_is_not_reachable_by_GET(make_client, node):
    assert make_client().get("/api/node/restart").status_code == 405
    assert node["writes"] == []
