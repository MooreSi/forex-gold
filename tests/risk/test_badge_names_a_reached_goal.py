"""A day stopped by its own profit goal says so in the header.

Owner, 2026-10-05: the header read "VPS: Trading Paused until 05 Oct 22:00"
after the daily goal was reached, and should read "VPS: Goal Achieved Paused
until <date/time>". The daily goal halts through the same `trade_pause_until`
pair as the loss guards, so without this the badge cannot tell a good day
from a drawdown: the same words for opposite news.

The goal's label applies only when the goal is the ONLY thing holding entries.
A tripped circuit breaker is a loss guard and keeps "Trading Paused".

Nothing here places an order or reaches a broker.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster.sync import client as sync_client
from backend.src.services.cluster.sync.protocol import TRADER_REMOTE_VPS
from backend.src.services.risk import daily_goal
from backend.src.services.risk import trading_status as ts

_NOW = 1_758_000_000.0
_SECURED = "Daily goal secured: +$30.00 today vs a goal of $25.00"
_HOLD = ("Daily goal reached (+$30.00 today vs a goal of $25.00), "
         "waiting for 2 open trades to close")


@pytest.fixture
def lab(monkeypatch):
    state = {"breaker": {"is_active": False, "active_until": 0.0,
                         "losses_threshold": 3},
             "pause_until": _NOW + 3600, "halt_reason": _SECURED}
    monkeypatch.setattr(ts, "_paired_vps_view", lambda: None)
    monkeypatch.setattr(ts, "_breaker_state", lambda: dict(state["breaker"]))
    monkeypatch.setattr(ts, "_trade_pause_until", lambda: state["pause_until"])
    monkeypatch.setattr(ts, "_halt_reason", lambda: state["halt_reason"])
    monkeypatch.setattr(ts, "_daily_target_state", lambda: {"reached": False})
    monkeypatch.setattr(ts, "_news_state", lambda: {"paused": False})
    monkeypatch.setattr(ts.time, "time", lambda: _NOW)
    return state


class TestOnTheTradingNode:
    def test_a_secured_goal_reads_goal_achieved(self, lab):
        out = ts.badge()

        assert out["state"] == "halted"
        assert out["label"] == f"Goal Achieved Paused until {ts._until_text(_NOW + 3600)}"

    def test_a_goal_held_for_open_trades_reads_the_same(self, lab):
        lab["halt_reason"] = _HOLD

        assert ts.badge()["label"].startswith("Goal Achieved Paused until ")

    def test_the_reason_is_still_the_detail(self, lab):
        assert ts.badge()["detail"] == _SECURED

    def test_a_loss_guard_still_reads_trading_paused(self, lab):
        """Negative control: the new label must not swallow the others."""
        lab["halt_reason"] = "Daily loss limit hit: $-178.45"

        assert ts.badge()["label"].startswith("Trading Paused until ")

    def test_a_tripped_breaker_beside_the_goal_reads_trading_paused(self, lab):
        lab["breaker"] = {"is_active": True, "active_until": _NOW + 600,
                          "losses_threshold": 3}

        assert ts.badge()["label"].startswith("Trading Paused until ")


_VPS_GOAL = {
    "state": "halted",
    # What a VPS on older code still sends. The Mac relabels from `detail`,
    # so the header is right before the VPS is updated.
    "label": "Trading Paused until 05 Oct 22:00",
    "detail": _SECURED,
    "until": _NOW + 3600, "resume_ts": None, "can_resume": True,
}


@pytest.fixture
def mac(fresh_db, monkeypatch):
    fresh_db.set_app_config("sync_remote_host", "203.0.113.7")
    fresh_db.set_app_config("active_trader", TRADER_REMOTE_VPS)
    cli = sync_client.get_instance()
    monkeypatch.setattr(cli, "conn_state", "connected")
    monkeypatch.setattr(cli, "remote_status", {"trading_status": dict(_VPS_GOAL)})
    return cli


class TestOnAMacThatTradesThroughTheVps:
    def test_the_reported_case(self, mac):
        out = ts.badge()

        assert out["label"] == (
            f"VPS: Goal Achieved Paused until {ts._until_text(_NOW + 3600)}")

    def test_a_vps_breaker_still_reads_trading_paused(self, mac):
        mac.remote_status = {"trading_status": {
            **_VPS_GOAL,
            "detail": f"{_SECURED} / Circuit breaker active (3 consecutive losses)"}}

        assert ts.badge()["label"].startswith("VPS: Trading Paused until ")

    def test_a_vps_loss_halt_still_reads_trading_paused(self, mac):
        mac.remote_status = {"trading_status": {
            **_VPS_GOAL, "detail": "Daily loss limit hit: $-178.45"}}

        assert ts.badge()["label"].startswith("VPS: Trading Paused until ")


def test_both_goal_halts_carry_the_prefix_the_badge_looks_for(fresh_db, monkeypatch):
    """The badge keys off the reason's opening words. If `daily_goal` rewords
    either halt, this fails rather than the header silently reverting."""
    monkeypatch.setattr(daily_goal._gov, "day_pnl_and_peak", lambda _ts: (30.0, 30.0))
    secured = daily_goal.check_daily_goal(
        {"daily_goal_enabled": 1, "daily_goal_value": 25, "daily_goal_mode": "usd"},
        None)

    assert secured and secured.startswith(ts.GOAL_REASON_PREFIX)
    assert daily_goal.HOLD_PREFIX.startswith(ts.GOAL_REASON_PREFIX)
