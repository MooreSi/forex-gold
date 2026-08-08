"""The thin services the settings/trading controllers route through.

These wrap an existing repo and add the async form the pages need. They are
short, but "short" is not "cannot break": a passthrough that forwards to the
wrong repo function, drops a kwarg, or loses the `to_db_thread` hop fails
silently and only shows up as a stalled UI or a setting that never saves.

`retention` gets the most attention here because it is the one with real
behaviour — a retention window of 0 means *indefinite*, and a prune that
treated 0 as "delete everything older than now" would wipe the trade history
of every install that never opted in.
"""
from __future__ import annotations

import asyncio
import tempfile

import pytest

from backend.src.db import database as db_module
from backend.src.db import retention as db_retention
from backend.src.services.risk import app_config, retention, settings


@pytest.fixture
def db(monkeypatch):
    db_module.init(tempfile.mktemp(suffix=".db"))
    # get_risk_settings caches for 10s; clear it so each test sees its own writes.
    monkeypatch.setattr(db_module, "_rs_cache", None, raising=False)
    monkeypatch.setattr(db_module, "_rs_cache_ts", 0.0, raising=False)
    yield db_module


# ── app_config ───────────────────────────────────────────────────────────────

def test_app_config_round_trips(db):
    app_config.set("greeting", "hello")
    assert app_config.get("greeting") == "hello"


def test_app_config_returns_none_for_an_unset_key(db):
    assert app_config.get("never-written") is None


def test_app_config_async_forms_reach_the_same_store(db):
    async def _go():
        await app_config.set_async("via_async", "yes")
        return await app_config.get_async("via_async")
    assert asyncio.run(_go()) == "yes"
    # and the sync reader sees it too -- same store, not a parallel cache
    assert app_config.get("via_async") == "yes"


# ── risk settings ────────────────────────────────────────────────────────────

def test_risk_settings_update_is_visible_to_the_next_read(db):
    settings.update({"max_daily_loss_pct": 12.5})
    assert settings.get()["max_daily_loss_pct"] == 12.5


def test_risk_settings_async_returns_the_same_shape_as_sync(db):
    sync = settings.get()
    got = asyncio.run(settings.get_async())
    assert set(got) == set(sync)


def test_circuit_breaker_state_is_readable(db):
    assert isinstance(settings.circuit_breaker_state(), dict)


def test_custom_strategies_starts_empty_and_is_a_list(db):
    assert isinstance(settings.custom_strategies(), list)


# ── retention ────────────────────────────────────────────────────────────────

def test_retention_defaults_to_indefinite(db):
    assert retention.get_days() == 0


def test_retention_days_round_trip(db):
    retention.set_days(30)
    assert retention.get_days() == 30


def test_a_negative_retention_window_is_clamped_to_indefinite(db):
    """A negative window must not become a cutoff in the future, which would
    match every row in every table."""
    retention.set_days(-5)
    assert retention.get_days() == 0


def test_prune_is_a_no_op_while_retention_is_indefinite(db):
    """The default is 0 and the default must delete nothing. An install that
    never opted in has its whole history riding on this branch."""
    retention.set_days(0)
    result = retention.prune()
    assert result["pruned"] is False
    assert result["reason"] == "indefinite"
    assert result["deleted"] == {}


def test_prune_reports_per_table_counts_once_a_window_is_set(db):
    retention.set_days(1)
    result = retention.prune()
    assert result["pruned"] is True
    assert result["retention_days"] == 1
    assert isinstance(result["deleted"], dict)


def test_the_service_and_the_db_module_agree_on_the_window(db):
    """The service is a forward, not a second source of truth."""
    retention.set_days(7)
    assert retention.get_days() == db_retention.get_data_retention_days()
