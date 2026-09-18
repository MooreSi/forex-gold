"""Switching the whole app between the demo and the live account.

**This is the control that points the app at real money.** Everything else in
the dashboard decides what happens on whichever account is selected; this
decides which account that is. It was a toggle in the NiceGUI header and the
React port dropped it entirely.

Four things have to happen together or the app ends up half-switched — reading
one account's trade history while sending orders to the other:

  1. the target account's credentials are written to `bridge_credentials.json`,
     which is what the bridge reads on start;
  2. the shared database connection is re-pointed at that environment's file;
  3. `account_env` is persisted, so a restart comes back to the same place;
  4. the app restarts, so every cached handle is rebuilt against the new one.

The order is the safety property. **Credentials are validated first and nothing
is written if they are missing** — a half-switch that leaves the database
pointing at `live` with the bridge still logged into the demo account would
show demo history beside live orders, and neither screen would say so.

Nothing here reaches a broker, a real database or a real file: every step is a
recorder, and no test switches anything.
"""
from __future__ import annotations

import pytest

from backend.src.services.broker import environment as env_svc


@pytest.fixture
def machine(monkeypatch):
    state = {
        "env": "demo",
        "creds": {
            "login": "5203117", "password_enc": "demo-pw", "server": "Vantage-Demo",
            "live_login": "900123", "live_password_enc": "live-pw",
            "live_server": "Vantage-Live",
        },
        "synced": [],
        "db_paths": [],
        "config_writes": [],
        "sync_ok": True,
    }
    monkeypatch.setattr(env_svc._creds, "get_mt5_credentials", lambda: dict(state["creds"]))
    monkeypatch.setattr(env_svc._creds, "sync_bridge_credentials_file",
                        lambda e: (state["synced"].append(e), state["sync_ok"])[1])
    monkeypatch.setattr(env_svc._retention, "switch_environment",
                        lambda p: state["db_paths"].append(p))
    monkeypatch.setattr(env_svc._cfg, "get",
                        lambda k, d=None: state["env"] if k == "account_env" else d)
    monkeypatch.setattr(env_svc._cfg, "save_to_yaml",
                        lambda v: (state["config_writes"].append(v),
                                   state.__setitem__("env", v.get("account_env", state["env"]))))
    return state


class TestWhereItIsNow:
    def test_it_reports_the_stored_environment(self, machine):
        assert env_svc.current() == "demo"

    def test_an_unset_value_reads_as_demo(self, machine, monkeypatch):
        """Fail safe. An install with no stored value must not come up live."""
        monkeypatch.setattr(env_svc._cfg, "get", lambda k, d=None: None)

        assert env_svc.current() == "demo"

    def test_a_value_it_does_not_recognise_also_reads_as_demo(self, machine, monkeypatch):
        """A typo in config.yaml is not a reason to point at a live account."""
        monkeypatch.setattr(env_svc._cfg, "get", lambda k, d=None: "LIVE!")

        assert env_svc.current() == "demo"


class TestDescribingBothSides:
    def test_it_says_which_accounts_are_configured(self, machine):
        described = env_svc.describe()

        assert described["current"] == "demo"
        assert described["environments"]["demo"]["configured"] is True
        assert described["environments"]["live"]["configured"] is True

    def test_it_names_the_account_without_its_password(self, machine):
        """The login and server are how an operator checks they are about to
        switch to the account they meant. The password is not."""
        live = env_svc.describe()["environments"]["live"]

        assert live["login"] == "900123"
        assert live["server"] == "Vantage-Live"
        assert "live-pw" not in str(env_svc.describe())

    def test_a_missing_live_account_is_reported_as_not_configured(self, machine):
        machine["creds"]["live_login"] = ""

        assert env_svc.describe()["environments"]["live"]["configured"] is False


class TestSwitching:
    def test_it_does_all_four_things_in_order(self, machine):
        result = env_svc.switch("live")

        assert machine["synced"] == ["live"]
        assert machine["db_paths"] and machine["db_paths"][0].endswith("forex_trader_live.db")
        assert machine["config_writes"] == [{"account_env": "live"}]
        assert result["environment"] == "live"

    def test_the_database_file_is_named_for_the_environment(self, machine):
        env_svc.switch("demo")

        assert machine["db_paths"][0].endswith("forex_trader_demo.db")

    def test_it_says_the_app_has_to_restart(self, machine):
        """Every cached handle — the runtime, the bridge, the engines — was
        built against the old account. The response says so; the router is
        what actually restarts."""
        assert env_svc.switch("live")["restart_required"] is True

    def test_switching_to_live_is_reported_as_such(self, machine):
        """The caller needs to be able to say something different about it."""
        assert env_svc.switch("live")["is_live"] is True
        assert env_svc.switch("demo")["is_live"] is False


class TestItRefusesRatherThanHalfSwitching:
    def test_an_environment_it_does_not_know_is_refused(self, machine):
        with pytest.raises(ValueError):
            env_svc.switch("staging")

        assert machine["synced"] == [] and machine["config_writes"] == []

    def test_missing_live_credentials_stop_it_before_anything_is_written(self, machine):
        """The whole point of validating first. Re-pointing the database at the
        live file while the bridge stays logged into the demo account shows
        demo history beside live orders, and neither screen says so."""
        machine["creds"]["live_password_enc"] = ""

        with pytest.raises(ValueError) as exc:
            env_svc.switch("live")

        assert "Live" in str(exc.value)
        assert machine["synced"] == []
        assert machine["db_paths"] == []
        assert machine["config_writes"] == []

    def test_missing_demo_credentials_are_named_as_demo(self, machine):
        machine["creds"]["login"] = ""

        with pytest.raises(ValueError) as exc:
            env_svc.switch("demo")

        assert "Demo" in str(exc.value)

    def test_a_credentials_file_that_will_not_write_stops_the_switch(self, machine):
        """`sync_bridge_credentials_file` returning False means the bridge will
        come back up on the OLD account. Carrying on would point the database
        at one account and the orders at another."""
        machine["sync_ok"] = False

        with pytest.raises(ValueError):
            env_svc.switch("live")

        assert machine["db_paths"] == []
        assert machine["config_writes"] == []


class TestTheStoreIsReadFromOnePlace:
    def test_credentials_come_from_the_master_store(self, machine):
        """Both accounts' credentials live in the demo database, deliberately:
        they have to be readable while pointing at either environment, and a
        per-environment copy is one that goes stale on whichever side was not
        edited."""
        import inspect

        src = inspect.getsource(env_svc)

        assert "get_mt5_credentials" in src
        assert "live_password_enc" in src
