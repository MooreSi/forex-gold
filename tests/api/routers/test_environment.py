"""Demo or live: the control that decides whether the account holds real money.

Every other setting in this app decides what happens on whichever account is
selected. This one decides which account that is, so the tests here are about
the two things that stop it being used by accident or left half-done:

  * **Live needs an explicit confirmation.** Nothing else in this API does.
  * **A refusal leaves nothing changed**, and a restart that fails leaves the
    app correctly pointed with the operator told to restart it.

Nothing here switches anything: the service is a recorder and the runtime is
the suite's sentinel. No test writes a credentials file, re-points a database
or restarts a process.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import environment as env_router


@pytest.fixture
def env(monkeypatch):
    state = {
        "described": {
            "current": "demo",
            "environments": {
                "demo": {"login": "5203117", "server": "Vantage-Demo", "configured": True},
                "live": {"login": "900123", "server": "Vantage-Live", "configured": True},
            },
        },
        "switched": [],
        "restarts": [],
        "switch_error": None,
        "restart_error": None,
    }

    def _switch(target):
        if state["switch_error"]:
            raise ValueError(state["switch_error"])
        state["switched"].append(target)
        return {"environment": target, "is_live": target == "live",
                "login": "900123", "server": "Vantage-Live",
                "restart_required": True, "note": "pointed at it"}

    async def _restart(engine):
        if state["restart_error"]:
            raise state["restart_error"]
        state["restarts"].append(True)
        return "restarting"

    monkeypatch.setattr(env_router.env_ctl, "describe_environments",
                        lambda: state["described"])
    monkeypatch.setattr(env_router.env_ctl, "switch_environment", _switch)
    monkeypatch.setattr(env_router.node_ctl, "restart_app", _restart)
    return state


def _message(res) -> str:
    return res.json()["error"]["message"]


class TestReading:
    def test_it_says_which_account_is_active(self, make_client, env):
        assert make_client().get("/api/settings/environment").json()["current"] == "demo"

    def test_it_names_both_accounts_so_the_operator_can_check(self, make_client, env):
        """The login and server are how somebody confirms they are about to
        switch to the account they meant."""
        body = make_client().get("/api/settings/environment").json()

        assert body["environments"]["live"]["login"] == "900123"
        assert body["environments"]["live"]["server"] == "Vantage-Live"

    def test_each_account_reports_only_what_the_operator_needs(self, make_client, env):
        """Login, server and whether it is usable. Not the password.

        Asserted as an exact field set rather than "the password is absent":
        the absence of one name says nothing about the next credential
        somebody adds to the credentials row.
        """
        body = make_client().get("/api/settings/environment").json()

        for name, account in body["environments"].items():
            assert set(account) == {"login", "server", "configured"}, name


class TestSwitchingToLiveNeedsSayingSoTwice:
    def test_without_confirmation_it_is_refused_and_nothing_changes(
        self, make_client, env,
    ):
        res = make_client().put("/api/settings/environment",
                                json={"environment": "live"})

        assert res.status_code == 400
        assert "real money" in _message(res)
        assert env["switched"] == []
        assert env["restarts"] == []

    def test_with_confirmation_it_goes_through(self, make_client, env):
        res = make_client().put("/api/settings/environment",
                                json={"environment": "live", "confirm": True})

        assert res.status_code == 200
        assert env["switched"] == ["live"]

    def test_demo_does_not_need_confirming(self, make_client, env):
        """The safe direction. Asking twice for it would train the operator to
        click through the question that matters."""
        res = make_client().put("/api/settings/environment",
                                json={"environment": "demo"})

        assert res.status_code == 200
        assert env["switched"] == ["demo"]


class TestItRestartsAfterwards:
    def test_the_app_restarts_once_the_switch_is_recorded(self, make_client, env):
        """Every cached handle — the runtime, the bridge, the engines — was
        built against the old account."""
        make_client().put("/api/settings/environment", json={"environment": "demo"})

        assert env["restarts"] == [True]

    def test_a_restart_that_fails_leaves_the_app_pointed_and_says_so(
        self, make_client, env,
    ):
        """The switch is already recorded, so reporting failure for the whole
        operation would be wrong. The operator is told to restart it."""
        env["restart_error"] = RuntimeError("no supervisor")

        body = make_client().put("/api/settings/environment",
                                 json={"environment": "demo"}).json()

        assert body["environment"] == "demo"
        assert "Restart it before trading" in body["restart"]

    def test_nothing_restarts_when_the_switch_was_refused(self, make_client, env):
        env["switch_error"] = "No Live MT5 credentials are saved."

        make_client().put("/api/settings/environment",
                          json={"environment": "live", "confirm": True})

        assert env["restarts"] == []


class TestARefusalReachesTheOperator:
    def test_missing_credentials_are_reported_in_the_service_s_own_words(
        self, make_client, env,
    ):
        """"No Live MT5 credentials are saved" tells them what to do. "Invalid
        request" does not."""
        env["switch_error"] = (
            "No Live MT5 credentials are saved. Enter the Live account's login, "
            "password and server under Settings > MT5, then switch again.")

        res = make_client().put("/api/settings/environment",
                                json={"environment": "live", "confirm": True})

        assert res.status_code == 400
        assert "Settings > MT5" in _message(res)

    def test_an_unknown_environment_is_refused(self, make_client, env):
        env["switch_error"] = "Unknown environment 'staging'."

        res = make_client().put("/api/settings/environment",
                                json={"environment": "staging", "confirm": True})

        assert res.status_code == 400


def test_reading_never_switches_anything(make_client, env):
    """Negative control. This endpoint is polled by a settings screen."""
    make_client().get("/api/settings/environment")

    assert env["switched"] == [] and env["restarts"] == []


def test_switching_is_not_reachable_by_a_get(make_client, env):
    assert make_client().get("/api/settings/environment").status_code == 200
    assert make_client().post("/api/settings/environment",
                              json={"environment": "live"}).status_code == 405
    assert env["switched"] == []
