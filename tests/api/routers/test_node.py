"""Node pairing, autostart, restart and applying a release.

Most of it does not trade; all of it can stop the app trading. `/active-trader`
is the exception and has its own section at the bottom: it decides which of two
paired nodes may open positions against the shared account.

The two assertions that matter elsewhere are about a secret and a one-way
action:

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
        # The real shape of `check_for_update`. This said {"version": "1.5.0"},
        # which that function never returns -- and the `summarise_changes`
        # stub below was a SYNC one-argument lambda, which it never was
        # either. Between them the handler could call an async two-argument
        # function with one positional dict, never await it, and unpack its
        # tuple as a list, and every test here stayed green. It 500'd on the
        # running app for any install with an update pending. Corrected
        # 2026-09-20.
        "update": {
            "available": True, "local_sha": "aaaaaaa", "remote_sha": "bbbbbbb",
            "commits": [{"sha": "bbbbbbb", "short_sha": "bbbbbbb",
                         "summary": "Ported the News tab"}],
            "error": None,
        },
        "writes": [],
        # Kept out of `writes`: that list is what
        # `test_checking_for_an_update_never_applies_one` asserts is empty,
        # and a summary is a read, not a write.
        "summaries": [],
        "handover": [],
        "refuse": "",
    }

    async def _check():
        return state["update"]

    async def _apply():
        state["writes"].append(("apply_update",))
        return {"ok": True}

    async def _restart(engine):
        state["writes"].append(("restart", engine))
        return "restarting"

    async def _stop(engine):
        state["writes"].append(("stop", engine))
        return "stopping"

    monkeypatch.setattr(node_router.system_ctl, "app_version", lambda: "1.4.2")
    monkeypatch.setattr(node_router.system_ctl, "check_for_update", _check)
    monkeypatch.setattr(node_router.system_ctl, "apply_update", _apply)
    async def _summarise(local_sha, remote_sha, *a, **kw):
        state["summaries"].append((local_sha, remote_sha))
        return ["Ported the News tab"], ""

    monkeypatch.setattr(node_router.system_ctl, "summarise_changes", _summarise)
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
    monkeypatch.setattr(node_router.node_ctl, "stop_app", _stop)
    monkeypatch.setattr(node_router.remote_ctl, "get_status", lambda: state["registration"])
    monkeypatch.setattr(node_router.remote_ctl, "get_stored_email", lambda: state["email"])
    monkeypatch.setattr(node_router.remote_ctl, "request_registration",
                        lambda email, nickname: state["writes"].append(("register", email, nickname)))

    # The handover, recorded rather than run. Its own sequence is tested in
    # tests/services/cluster/test_handover.py; here the claim is only that the
    # handler goes through it instead of writing the flag itself.
    async def _take_over(*a, **k):
        state["handover"].append("take_over")
        if state["refuse"]:
            raise node_router.sync_ctl.HandoverRefused(state["refuse"])
        return {"active_trader": "local", "remote_open_positions": 0, "note": "ok"}

    async def _hand_back(*a, open_trades=None, **k):
        state["handover"].append("hand_back")
        if state["refuse"]:
            raise node_router.sync_ctl.HandoverRefused(state["refuse"])
        return {"active_trader": "remote_vps",
                "local_open_positions": len(open_trades or []), "note": "ok"}

    monkeypatch.setattr(node_router.sync_ctl, "take_over_locally", _take_over)
    monkeypatch.setattr(node_router.sync_ctl, "hand_back_to_remote", _hand_back)
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

def test_the_active_trader_is_reported(make_client, node):
    assert make_client().get("/api/node/state").json()["active_trader"] == "local"


class TestChangingItRunsTheHandshake:
    """**Not a flag write**, which is what it was between 2026-09-18 and this
    change — the most dangerous line in the React port. Setting `local` without
    the peer standing down leaves two nodes believing they own the same MT5
    account; setting `remote_vps` without stopping the local engines leaves
    them running. The sequence itself is tested in
    `tests/services/cluster/test_handover.py`; these are about the handler
    reaching it and reporting what it said."""

    def test_taking_over_goes_through_the_handover(self, make_client, node):
        body = make_client().put(
            "/api/node/active-trader", json={"trader": "local"}).json()

        assert node["handover"] == ["take_over"]
        assert body["active_trader"] == "local"
        # And NOT a bare set_active_trader behind its back.
        assert ("active_trader", "local") not in node["writes"]

    def test_handing_back_goes_through_the_handover(self, make_client, node):
        body = make_client().put(
            "/api/node/active-trader", json={"trader": "remote_vps"}).json()

        assert node["handover"] == ["hand_back"]
        assert body["active_trader"] == "remote_vps"

    def test_handing_back_reports_the_positions_that_keep_running(
        self, make_client, node, sentinel_engine,
    ):
        """Handing back closes nothing. "View-only" does not mean "flat"."""
        sentinel_engine.open_trades = [{"ticket": 1}, {"ticket": 2}]

        body = make_client().put(
            "/api/node/active-trader", json={"trader": "remote_vps"}).json()

        assert body["local_open_positions"] == 2

    def test_a_refusal_reaches_the_operator_in_its_own_words(
        self, make_client, node,
    ):
        """"The VPS did not acknowledge" is the difference between "try again"
        and "your account is being traded twice". A generic 500 loses it."""
        node["refuse"] = "The remote node did not acknowledge the stand-down."

        res = make_client().put("/api/node/active-trader", json={"trader": "local"})

        assert res.status_code == 409
        assert "did not acknowledge" in res.json()["error"]["message"]


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
    assert body["update"]["available"] is True
    assert body["update"]["remote_sha"] == "bbbbbbb"
    assert body["changes"] == ["Ported the News tab"]


def test_no_update_available_lists_no_changes(make_client, node):
    """Negative control: summarising costs a paid model call, and an install
    that is up to date has nothing to summarise."""
    node["update"] = {"available": False, "local_sha": "aaaaaaa",
                      "remote_sha": "aaaaaaa", "commits": [], "error": None}

    body = make_client().get("/api/node/update").json()

    assert body["update"]["available"] is False
    assert body["changes"] == []
    assert node["summaries"] == []


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


# -- The power button's other half --------------------------------------------

def test_stopping_goes_through_the_runtime(make_client, node, sentinel_engine):
    # Not by killing the process: the service persists the Telegram bot's
    # update offset first, so the next start does not replay the command that
    # caused the shutdown.
    body = make_client().post("/api/node/stop").json()

    assert body["result"] == "stopping"
    assert ("stop", sentinel_engine) in node["writes"]


def test_stop_is_not_reachable_by_GET(make_client, node):
    # A link a browser can prefetch must not shut the app down.
    assert make_client().get("/api/node/stop").status_code == 405


def test_stopping_is_not_restarting(make_client, node):
    make_client().post("/api/node/stop")

    assert not any(w[0] == "restart" for w in node["writes"])
