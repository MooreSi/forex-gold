"""On a Mac that hands its orders to the VPS, the badge reports the VPS.

Reported live, 2026-09-28: the circuit breaker tripped on the VPS (three live
losses, Telegram said so) and the Mac's header went on reading "Trading
Active". The badge read only the Mac's own database, and the Mac is not the
node placing orders -- so it was answering a question nobody asked, and
answering it with an all-clear.

"Hands its orders to the VPS" is the test `open_trade` itself uses before
forwarding an order: a paired host (`sync_remote_host`) and the switch on
REMOTE. The VPS has no host of its own and so always reports itself.

When the Mac cannot see the VPS's answer -- link down, or a VPS too old to
send one -- the badge says it does not know. Falling back to the Mac's own
state is the bug this file exists for.

Nothing here places an order or reaches a broker.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster.sync import client as sync_client
from backend.src.services.cluster.sync.protocol import TRADER_LOCAL, TRADER_REMOTE_VPS
from backend.src.services.cluster.sync._telemetry import TelemetryMixin
from backend.src.services.risk import trading_status as ts

_NOW = 1_758_000_000.0

_VPS_HALTED = {
    "state": "halted",
    "label": "Trading Paused until 16 Sep 06:15",
    "detail": "Circuit breaker active (3 consecutive losses)",
    "until": _NOW + 900, "resume_ts": None, "can_resume": True,
}
_VPS_OK = {
    "state": "ok", "label": "Trading Active",
    "detail": "Nothing is holding automated entries.",
    "until": None, "resume_ts": None, "can_resume": False,
}


@pytest.fixture
def mac(fresh_db, monkeypatch):
    """A Mac paired with a VPS, on REMOTE, link up, nothing held locally."""
    fresh_db.set_app_config("sync_remote_host", "203.0.113.7")
    fresh_db.set_app_config("active_trader", TRADER_REMOTE_VPS)
    cli = sync_client.get_instance()
    monkeypatch.setattr(cli, "conn_state", "connected")
    monkeypatch.setattr(cli, "remote_status", {"trading_status": dict(_VPS_OK)})
    monkeypatch.setattr(ts, "_breaker_state",
                        lambda: {"is_active": False, "active_until": 0.0,
                                 "losses_threshold": 3})
    monkeypatch.setattr(ts, "_trade_pause_until", lambda: 0.0)
    monkeypatch.setattr(ts, "_daily_target_state", lambda: {"reached": False})
    monkeypatch.setattr(ts, "_news_state", lambda: {"paused": False})
    monkeypatch.setattr(ts.time, "time", lambda: _NOW)
    return cli


def test_the_reported_case_a_breaker_tripped_on_the_vps(mac):
    mac.remote_status = {"trading_status": dict(_VPS_HALTED)}

    out = ts.badge()

    assert out["state"] == "halted"
    assert out["label"].startswith("VPS: Trading Paused until ")
    assert "Circuit breaker active" in out["detail"]
    assert out["node"] == "vps"


def test_the_pause_time_is_written_in_this_nodes_clock(mac):
    """The VPS formats `until` in ITS time zone. The operator reads the Mac."""
    mac.remote_status = {"trading_status": dict(_VPS_HALTED)}

    out = ts.badge()

    assert out["label"] == f"VPS: Trading Paused until {ts._until_text(_NOW + 900)}"


def test_a_clear_vps_reads_as_active_and_says_whose_status_it_is(mac):
    out = ts.badge()

    assert out["state"] == "ok"
    assert out["label"] == "VPS: Trading Active"


def test_the_macs_own_resume_is_not_offered(mac):
    """A Resume or Pause pressed here would act on this Mac's database, and
    this Mac is not placing the orders."""
    mac.remote_status = {"trading_status": dict(_VPS_HALTED)}

    assert ts.badge()["can_resume"] is False


def test_a_local_hold_on_the_mac_does_not_mask_the_vps(mac, monkeypatch):
    monkeypatch.setattr(ts, "_breaker_state",
                        lambda: {"is_active": True, "active_until": _NOW + 60,
                                 "losses_threshold": 3})

    out = ts.badge()

    assert out["state"] == "ok"
    assert out["node"] == "vps"


def test_the_link_down_is_unknown_never_active(mac):
    mac.conn_state = "disconnected"

    out = ts.badge()

    assert out["state"] == "unknown"
    assert "VPS" in out["label"]
    assert out["can_resume"] is False


def test_a_vps_that_sends_no_status_is_unknown(mac):
    """An older VPS, or the moment before its first heartbeat."""
    mac.remote_status = {"balance": 1000.0}

    out = ts.badge()

    assert out["state"] == "unknown"


def test_on_local_the_mac_reports_itself(mac, fresh_db, monkeypatch):
    fresh_db.set_app_config("active_trader", TRADER_LOCAL)
    monkeypatch.setattr(ts, "_breaker_state",
                        lambda: {"is_active": True, "active_until": _NOW + 60,
                                 "losses_threshold": 3})

    out = ts.badge()

    assert out["state"] == "halted"
    assert "node" not in out
    assert not out["label"].startswith("VPS")


def test_the_vps_itself_reports_itself(mac, fresh_db):
    """The VPS has no remote host; its active_trader also reads remote_vps."""
    fresh_db.set_app_config("sync_remote_host", "")

    out = ts.badge()

    assert "node" not in out
    assert out["label"] == "Trading Active"


def test_not_knowing_which_node_trades_is_unknown(mac, monkeypatch):
    def boom():
        raise RuntimeError("database is locked")
    monkeypatch.setattr(ts, "_paired_vps_view", boom)

    assert ts.badge()["state"] == "unknown"


# ── The VPS's half: it has to send the answer ────────────────────────────────

class _Server(TelemetryMixin):
    _main_engine = None


@pytest.mark.asyncio
async def test_the_heartbeat_carries_the_vps_badge(fresh_db, monkeypatch):
    monkeypatch.setattr(ts, "badge", lambda: dict(_VPS_HALTED))

    payload = await _Server()._status_payload()

    assert payload["trading_status"] == _VPS_HALTED


@pytest.mark.asyncio
async def test_a_failing_badge_does_not_stop_the_heartbeat(fresh_db, monkeypatch):
    def boom():
        raise RuntimeError("no")
    monkeypatch.setattr(ts, "badge", boom)

    payload = await _Server()._status_payload()

    assert payload["trading_status"] is None
    assert "ts" in payload


# ── The Dashboard's "halted" line reads the same answer ─────────────────────

def test_the_dashboard_halt_line_shows_the_vps_halt(mac):
    mac.remote_status = {"trading_status": dict(_VPS_HALTED)}

    out = ts.header_pause()

    assert out == {"paused": True, "reason": _VPS_HALTED["detail"],
                   "until": _VPS_HALTED["until"], "source": "VPS"}


def test_the_dashboard_halt_line_is_clear_when_the_vps_is(mac):
    assert ts.header_pause()["paused"] is False


def test_on_local_the_dashboard_halt_line_is_this_nodes(mac, fresh_db, monkeypatch):
    from backend.src.services.risk import pause_status
    fresh_db.set_app_config("active_trader", TRADER_LOCAL)
    local = {"paused": True, "reason": "daily loss", "until": _NOW + 60,
             "source": "governor"}
    monkeypatch.setattr(pause_status, "summary", lambda: dict(local))

    assert ts.header_pause() == local
