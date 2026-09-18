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
                        lambda: state.__setitem__("synced", state["synced"] + 1))
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
        "login": "9001", "password": "new-one", "server": "Vantage-Live",
    })

    assert ("mt5", ("9001", "new-one", "Vantage-Live"), {}) in config["writes"]
    assert config["synced"] == 1


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
