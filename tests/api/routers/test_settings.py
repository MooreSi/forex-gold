"""Settings: twelve domains, and the one rule that cannot be got wrong twice.

**A secret must never reach a browser.** It cannot be taken back once it has,
so the redaction here is a denylist rather than an allowlist: a service that
grows a new credential field is excluded by default, and the test that proves
it plants a field nothing in this codebase has ever had.

The other assertions are about honesty of echo. Risk settings are clamped by
the service, so a write that echoed the REQUEST would show the operator a
number the engine is not using — the same class of mistake as the blackout
settings that saved four keys nothing read.

Nothing here places an order. Changing a risk setting changes what the engines
are allowed to do next time; it touches no open position.

The two outbound connections moved to `test_notifications.py` on 2026-09-18,
with their handlers. The redaction they are subject to is the same one, from
the same `api/redaction.py`, and it is asserted on both sides.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import settings as settings_router


@pytest.fixture
def config(monkeypatch):
    state = {
        "risk": {"risk_pct": 1.0, "max_open_trades": 3},
        "app": {"port": 8888, "claude_api_key": "sk-secret", "ai_provider": "anthropic"},
        "mt5": {"login": "5203117", "server": "Vantage-Demo", "password": "hunter2"},
        "retention": 90,
        "catalogue": {"re_min_adx": {"value": 22, "default": 20}},
        "log": [("12:00:01", "Engine started")],
        "breaker": {"tripped": False},
        "writes": [],
        "synced": 0,
    }

    def _record(name):
        def _fn(*args, **kwargs):
            state["writes"].append((name, args, kwargs))
        return _fn

    async def _breaker():
        return state["breaker"]

    async def _log():
        return state["log"]

    monkeypatch.setattr(settings_router.settings_ctl, "get_risk_settings", lambda: state["risk"])
    monkeypatch.setattr(settings_router.settings_ctl, "update_risk_settings", _record("risk"))
    monkeypatch.setattr(settings_router.settings_ctl, "load_config", lambda: state["app"])
    monkeypatch.setattr(settings_router.settings_ctl, "save_config", _record("app"))
    monkeypatch.setattr(settings_router.settings_ctl, "get_mt5_credentials", lambda: state["mt5"])
    monkeypatch.setattr(settings_router.settings_ctl, "save_mt5_credentials", _record("mt5"))
    monkeypatch.setattr(settings_router.settings_ctl, "sync_bridge_credentials_file",
                        lambda *a: state.__setitem__("synced", state["synced"] + 1))
    monkeypatch.setattr(settings_router.env_ctl, "describe_environments",
                        lambda: {"current": "demo", "environments": {}})
    monkeypatch.setattr(settings_router.settings_ctl, "get_data_retention_days",
                        lambda: state["retention"])
    monkeypatch.setattr(settings_router.settings_ctl, "set_data_retention_days",
                        _record("retention"))
    monkeypatch.setattr(settings_router.settings_ctl, "get_expert_param_catalogue",
                        lambda: state["catalogue"])
    monkeypatch.setattr(settings_router.settings_ctl, "save_expert_params",
                        lambda v: state["writes"].append(("expert", v)) or state["catalogue"])
    monkeypatch.setattr(settings_router.settings_ctl, "reset_expert_param",
                        lambda k: state["writes"].append(("reset_one", k)) or state["catalogue"])
    monkeypatch.setattr(settings_router.settings_ctl, "reset_all_expert_params",
                        lambda: state["writes"].append(("reset_all",)) or state["catalogue"])
    monkeypatch.setattr(settings_router.settings_ctl, "live_log_lines", _log)
    monkeypatch.setattr(settings_router.settings_ctl, "get_circuit_breaker_state_async", _breaker)
    monkeypatch.setattr(settings_router.settings_ctl, "reset_circuit_breaker",
                        _record("breaker_reset"))
    return state


# ── Secrets never leave ──────────────────────────────────────────────────────

@pytest.mark.parametrize("path,secret", [
    ("/api/settings/app", "sk-secret"),
    ("/api/settings/mt5", "hunter2"),
])
def test_no_secret_reaches_the_browser(path, secret, make_client, config):
    """The one that cannot be got wrong twice: a password in a response cannot
    be taken back."""
    body = make_client().get(path).text

    assert secret not in body


def test_a_configured_secret_is_reported_as_set_without_its_value(make_client, config):
    """`""` and `null` both read as "no password configured", so the flag is
    separate from the value."""
    body = make_client().get("/api/settings/mt5").json()

    assert body["password_set"] is True
    assert "password" not in body
    assert body["login"] == "5203117"


def test_an_absent_secret_is_reported_as_not_set(make_client, config):
    config["mt5"] = {"login": "5203117", "server": "Vantage-Demo", "password": ""}

    assert make_client().get("/api/settings/mt5").json()["password_set"] is False


def test_a_credential_field_this_codebase_has_never_had_is_still_redacted(
    make_client, config,
):
    """The denylist's whole reason. A service that grows a new secret must be
    excluded by default, not after somebody remembers to add it."""
    config["app"] = {"port": 8888, "broker_api_passphrase": "letmein"}

    body = make_client().get("/api/settings/app").json()

    assert "letmein" not in str(body)
    assert body["broker_api_passphrase_set"] is True


def test_an_ordinary_field_is_not_redacted(make_client, config):
    """Negative control: a redactor that hid everything would pass every test
    above and make the tab useless."""
    body = make_client().get("/api/settings/app").json()

    assert body["port"] == 8888
    assert body["ai_provider"] == "anthropic"


# ── Honest echoes ────────────────────────────────────────────────────────────

def test_a_risk_write_echoes_what_was_STORED_not_what_was_sent(make_client, config):
    """The service clamps. Echoing the request would show the operator a number
    the engine is not using."""
    config["risk"] = {"risk_pct": 2.0, "max_open_trades": 3}

    body = make_client().put("/api/settings/risk", json={"risk_pct": 99.0}).json()

    assert body["risk_pct"] == 2.0
    assert ("risk", ({"risk_pct": 99.0},), {}) in config["writes"]


def test_a_risk_write_forwards_only_the_fields_it_was_given(make_client, config):
    make_client().put("/api/settings/risk", json={"max_open_trades": 5})

    assert config["writes"] == [("risk", ({"max_open_trades": 5},), {})]


# ── MT5 ──────────────────────────────────────────────────────────────────────

def test_saving_mt5_credentials_also_syncs_the_bridge_file(make_client, config):
    """Saved here and not synced leaves the bridge authenticating as the
    previous account — the same shape as backing up the wrong database: it
    looks like it worked."""
    make_client().put("/api/settings/mt5", json={
        "login": "9001", "password": "new-one", "server": "Vantage-Demo",
    })

    assert ("mt5", ({"login": "9001", "password_enc": "new-one",
                     "server": "Vantage-Demo"},), {}) in config["writes"]
    assert config["synced"] == 1


def test_it_writes_one_dict_under_the_column_names_the_store_uses(
    make_client, config,
):
    """Three positional arguments to a one-dict save raised on every attempt,
    so MT5 credentials could not be set from the dashboard at all. And the
    password column is `password_enc` — which is also what the repo encrypts on
    the way in, so a value written as `password` would miss both."""
    make_client().put("/api/settings/mt5", json={
        "login": "9001", "password": "new-one", "server": "Vantage-Demo",
    })

    written = next(a[0] for name, a, _k in config["writes"] if name == "mt5")
    assert set(written) == {"login", "password_enc", "server"}


def test_the_live_account_is_stored_under_its_own_fields(make_client, config):
    """Both accounts share one row — they have to be readable while the app is
    pointed at either — so the field names differ rather than the table."""
    make_client().put("/api/settings/mt5", json={
        "login": "900123", "password": "live-pw", "server": "Vantage-Live",
        "environment": "live",
    })

    written = next(a[0] for name, a, _k in config["writes"] if name == "mt5")
    assert set(written) == {"live_login", "live_password_enc", "live_server"}


def test_editing_the_OTHER_account_does_not_rewrite_the_bridge_file(
    make_client, config,
):
    """The app is pointed at demo. Rewriting the bridge's credentials after
    editing the live account would hand it an account nobody asked it to use."""
    make_client().put("/api/settings/mt5", json={
        "login": "900123", "password": "live-pw", "server": "Vantage-Live",
        "environment": "live",
    })

    assert config["synced"] == 0


def test_an_unknown_environment_is_refused(make_client, config):
    res = make_client().put("/api/settings/mt5", json={
        "login": "1", "password": "p", "server": "s", "environment": "staging",
    })

    assert res.status_code == 400
    assert not [w for w in config["writes"] if w[0] == "mt5"]


def test_the_mt5_response_still_carries_no_password(make_client, config):
    body = make_client().put("/api/settings/mt5", json={
        "login": "9001", "password": "new-one", "server": "Vantage-Live",
    }).text

    assert "new-one" not in body


# ── Retention ────────────────────────────────────────────────────────────────

def test_retention_is_read_and_written(make_client, config):
    assert make_client().get("/api/settings/retention").json() == {"days": 90}

    make_client().put("/api/settings/retention", json={"days": 30})

    assert ("retention", (30,), {}) in config["writes"]


def test_a_retention_of_zero_is_refused_rather_than_deleting_everything(
    make_client, config,
):
    r = make_client().put("/api/settings/retention", json={"days": 0})

    assert r.status_code == 400
    assert config["writes"] == []


# ── Expert tunables ──────────────────────────────────────────────────────────

def test_the_tunables_screen_is_served_from_the_catalogue(make_client, config):
    """`/add-tunable` exists so a new tunable appears with no UI change. A
    hand-written form per parameter defeats it."""
    body = make_client().get("/api/settings/expert-params").json()

    assert body["re_min_adx"]["default"] == 20


def test_resetting_one_parameter_names_it(make_client, config):
    make_client().post("/api/settings/expert-params/reset", json={"key": "re_min_adx"})

    assert ("reset_one", "re_min_adx") in config["writes"]
    assert ("reset_all",) not in config["writes"]


def test_resetting_with_no_key_resets_them_all(make_client, config):
    make_client().post("/api/settings/expert-params/reset", json={})

    assert ("reset_all",) in config["writes"]


# ── Diagnostics ──────────────────────────────────────────────────────────────

def test_the_diagnostics_panel_reads_the_log_and_the_breaker_together(
    make_client, config,
):
    body = make_client().get("/api/settings/diagnostics").json()

    assert body["log"] == [["12:00:01", "Engine started"]]
    assert body["circuit_breaker"] == {"tripped": False}


def test_resetting_the_breaker_reports_the_state_afterwards(make_client, config):
    """Money-adjacent: it is what lets automated entries resume after a losing
    streak, so the operator sees the result rather than assuming it."""
    config["breaker"] = {"tripped": False, "losses": 0}

    body = make_client().post("/api/settings/circuit-breaker/reset").json()

    assert ("breaker_reset", (), {}) in config["writes"]
    assert body == {"tripped": False, "losses": 0}


def test_reading_diagnostics_never_resets_the_breaker(make_client, config):
    """Negative control. A poll that cleared the breaker would let a losing
    streak run for ever."""
    make_client().get("/api/settings/diagnostics")

    assert config["writes"] == []


# ── Who can open this app ────────────────────────────────────────────────────

class TestAppAccess:
    """Its own endpoint rather than a field on `/app`, for the reason the
    NiceGUI tab was its own tab: "does this machine ask for a password" is the
    first thing somebody looks for when they want to change it, and an access
    control buried in a page of unrelated settings is one that stays forgotten.

    Nothing here weakens the gate. `auto_login_enabled` still defaults to False
    and an unreadable config still keeps the door shut — this only writes the
    setting the gate reads."""

    def test_it_reports_the_current_setting(self, make_client, config, monkeypatch):
        monkeypatch.setattr(settings_router.settings_ctl, "get_config",
                            lambda k, d=None: False)

        assert make_client().get("/api/settings/access").json()["auto_login"] is False

    def test_automatic_login_comes_with_the_backend_s_own_warning(
        self, make_client, config, monkeypatch,
    ):
        """The one thing the operator has to weigh. Composing it in the browser
        means a UI that forgot it offers the choice without the consequence."""
        monkeypatch.setattr(settings_router.settings_ctl, "get_config",
                            lambda k, d=None: True)

        body = make_client().get("/api/settings/access").json()

        assert body["auto_login"] is True
        assert "without a password" in body["warning"]

    def test_requiring_the_password_carries_no_warning(
        self, make_client, config, monkeypatch,
    ):
        """Negative control: a warning shown always is a warning nobody reads."""
        monkeypatch.setattr(settings_router.settings_ctl, "get_config",
                            lambda k, d=None: False)

        assert make_client().get("/api/settings/access").json()["warning"] == ""

    def test_it_writes_the_key_the_gate_actually_reads(
        self, make_client, config, monkeypatch,
    ):
        """A setting written under a different name is a switch that does
        nothing — and this one's nothing is "the password is still required",
        which at least fails safe, but silently."""
        from backend.src.api import auth as auth_gate

        monkeypatch.setattr(settings_router.settings_ctl, "get_config",
                            lambda k, d=None: True)

        make_client().put("/api/settings/access", json={"auto_login": True})

        assert ("app", ({auth_gate.SETTING_KEY: True},), {}) in config["writes"]
