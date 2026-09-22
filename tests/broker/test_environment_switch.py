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


class _RecordingBridge:
    """A bridge that remembers what it was told and what it answered.

    Models the one thing that matters here: a bridge holds a LOGIN, it only
    changes when something tells it to, and it reports whatever it currently
    holds. `send_credentials` moves it, exactly as `_apply_credentials` in
    `mt5_bridge.py` does.
    """

    def __init__(self, login, *, accepts=True, answers=True):
        self.login = login
        self.accepts = accepts
        self.answers = answers
        self.sent: list[tuple] = []
        self.account_reads = 0

    async def get_account(self):
        self.account_reads += 1
        if not self.answers or self.login is None:
            return None
        return {"login": int(self.login), "server": "whatever", "is_demo": True}

    async def send_credentials(self, login, password, server):
        self.sent.append((login, password, server))
        if not self.accepts:
            return {"status": "credentials_saved_connect_failed", "error": "no"}
        self.login = login
        return {"status": "connected", "trade_allowed": True}


@pytest.mark.asyncio
class TestAligningTheRunningBridge:
    """The fourth step of the switch — "the app restarts, so every cached
    handle is rebuilt" — is only true of handles THIS process owns. On a Mac
    the bridge is a Wine subprocess with the MT5 terminal behind it, and
    `run._start_mt5_bridge` deliberately leaves a bridge that is already
    listening alone. So it outlives the restart still logged into the account
    the app just left: config, database and orders on live, the terminal on
    demo. Confirmed on the owner's Mac 2026-09-22, bridge up since 10:12,
    app restarted 17:17, account never moved.
    """

    async def test_a_bridge_on_the_wrong_account_is_sent_the_right_credentials(self, machine):
        machine["env"] = "live"
        bridge = _RecordingBridge(5203117)          # still the demo account

        await env_svc.align_bridge(bridge)

        assert bridge.sent == [(900123, "live-pw", "Vantage-Live")]

    async def test_it_reports_the_account_the_bridge_ended_up_on(self, machine):
        machine["env"] = "live"
        bridge = _RecordingBridge(5203117)

        result = await env_svc.align_bridge(bridge)

        assert result["status"] == "repointed"
        assert result["bridge_login"] == "900123"

    async def test_a_bridge_already_on_the_right_account_is_left_alone(self, machine):
        """Re-logging in costs a terminal round trip and resets AutoTrading.
        Every app restart would pay it."""
        bridge = _RecordingBridge(5203117)          # demo, and demo is current

        result = await env_svc.align_bridge(bridge)

        assert bridge.sent == []
        assert result["status"] == "aligned"

    async def test_a_bridge_that_does_not_answer_is_not_re_pointed(self, machine):
        """None is not evidence of the wrong account — it is no evidence at
        all, and a cold bridge answers nothing for up to ~150s. Logging one
        in on a guess is how a switch becomes an outage."""
        bridge = _RecordingBridge(None, answers=False)

        result = await env_svc.align_bridge(bridge)

        assert bridge.sent == []
        assert result["status"] == "unknown"

    async def test_a_login_the_bridge_refuses_is_reported_as_a_failure(self, machine):
        """Reported, not raised: the app must still start. But it must not
        claim the accounts agree when the terminal never moved."""
        machine["env"] = "live"
        bridge = _RecordingBridge(5203117, accepts=False)

        result = await env_svc.align_bridge(bridge)

        assert result["status"] == "failed"
        assert result["bridge_login"] == "5203117"

    async def test_missing_credentials_send_nothing(self, machine):
        machine["env"] = "live"
        machine["creds"]["live_password_enc"] = ""
        bridge = _RecordingBridge(5203117)

        result = await env_svc.align_bridge(bridge)

        assert bridge.sent == []
        assert result["status"] == "skipped"

    async def test_a_bridge_that_raises_never_stops_the_app_starting(self, machine):
        class _Broken:
            async def get_account(self):
                raise RuntimeError("bridge exploded")

        result = await env_svc.align_bridge(_Broken())

        assert result["status"] == "unknown"
