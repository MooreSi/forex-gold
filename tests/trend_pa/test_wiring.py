"""Trend PA reaches both nodes: the registry, the sync server's engine table
(heartbeat, Start/Stop, stand-down, the backtest button), the stats mirror,
and the panel's Remote-mode read. Nothing here reaches a broker.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster.sync import server as ss
from backend.src.services.engines import registry
from backend.src.services.trend_pa import panel_data
from backend.src.services.trend_pa import service


class _Ws:
    def __init__(self):
        self.sent: list = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class _Engine:
    def __init__(self):
        self.is_running = False
        self.calls: list = []

    def start(self):
        self.calls.append("start")
        self.is_running = True

    def stop(self, persist=True):
        self.calls.append(("stop", persist))
        self.is_running = False

    async def run_backtest(self):
        self.calls.append("backtest")
        return {"n": 0}


@pytest.fixture
def tpa(monkeypatch):
    eng = _Engine()
    monkeypatch.setattr(service, "_instance", eng)
    return eng


# ── the registry ─────────────────────────────────────────────────────────────

def test_it_is_a_named_engine_the_screen_shows(tpa):
    assert "trend_pa" in registry.ALL_NAMES
    assert "trend_pa" in registry.SHOWN_NAMES
    assert "trend_pa" not in registry.IMPLEMENTED_NAMES, "that list is the wire subset"
    assert "trend_pa" not in registry.ENGINE_NAMES, "ENGINE_NAMES is the wire order"
    assert registry.instance("trend_pa") is tpa


def test_the_wire_order_of_the_first_three_is_untouched():
    """server_start binds (breakout, bounce, reversal) positionally, and a
    paired node on an older build would see a fourth shift them."""
    assert registry.ENGINE_NAMES[:3] == ("breakout", "bounce", "reversal")
    assert len(registry.all_instances()) == 3


def test_a_mode_switch_starts_it_with_the_others(tpa, monkeypatch):
    for name in ("breakout", "reversal"):
        monkeypatch.setitem(registry._ENGINE_SERVICES, name, None)
    registry.start_stopped()
    assert tpa.calls == ["start"]


# ── the sync server ──────────────────────────────────────────────────────────

@pytest.fixture
def node():
    srv = ss.SyncServer.__new__(ss.SyncServer)
    srv._breakout_engine = None
    srv._re_engine = None
    srv._main_engine = None
    return srv


def test_the_server_lists_it_so_the_heartbeat_shows_it(node, tpa):
    assert node._sub_engines()["trend_pa"] is tpa


def test_the_mac_can_start_the_vps_copy(node, tpa):
    ws = _Ws()
    asyncio.run(node._handle_engine_control(ws, {"engine": "trend_pa", "action": "start"}))
    assert tpa.calls == ["start"] and "error" not in ws.sent[-1]


def test_the_backtest_button_is_acknowledged_before_the_backtest_finishes(node, tpa):
    """A three-year replay outlasts the 10s ack timeout; the ack must not wait."""
    ws = _Ws()

    async def go():
        await node._handle_engine_control(ws, {"engine": "trend_pa", "action": "backtest"})
        acked_before = "backtest" not in tpa.calls
        await asyncio.sleep(0)
        return acked_before
    assert asyncio.run(go()) is True
    assert "error" not in ws.sent[-1]
    assert tpa.calls == ["backtest"]


def test_backtest_is_refused_for_an_engine_that_has_none(node):
    class _Plain(_Engine):
        run_backtest = None
    node._breakout_engine = _Plain()
    ws = _Ws()
    asyncio.run(node._handle_engine_control(ws, {"engine": "breakout", "action": "backtest"}))
    assert "error" in ws.sent[-1]


def test_a_stand_down_stops_it_without_remembering_it(node, tpa, fresh_db):
    tpa.is_running = True
    ws = _Ws()
    asyncio.run(node._handle_stand_down(ws))
    assert ("stop", False) in tpa.calls


def test_the_stats_mirror_carries_it(node, monkeypatch):
    monkeypatch.setattr(panel_data, "local_report", lambda: {"marker": 1})
    for name in ("_breakout_stats", "_reversal_engine_stats"):
        monkeypatch.setattr(ss.SyncServer, name, staticmethod(lambda: {}))
    payload = asyncio.run(node._signal_gen_stats_payload())
    assert payload["trend_pa"] == {"marker": 1}
    assert set(payload) >= {"breakout", "bounce", "reversal_engine"}


# ── the panel ────────────────────────────────────────────────────────────────

def test_in_remote_mode_the_panel_reads_the_vps(monkeypatch):
    monkeypatch.setattr(panel_data._facade, "_is_remote_active", lambda: True)
    monkeypatch.setattr(panel_data._facade, "_remote_engine_stats",
                        lambda key: {"from": key})
    assert asyncio.run(panel_data.report())["from"] == "trend_pa"
    assert asyncio.run(panel_data.report())["where"] == "remote"


def test_locally_the_panel_reads_this_node(monkeypatch):
    monkeypatch.setattr(panel_data._facade, "_is_remote_active", lambda: False)
    monkeypatch.setattr(panel_data, "local_report", lambda: {"mine": True})
    r = asyncio.run(panel_data.report())
    assert r["mine"] is True and r["where"] == "local"


def test_running_reports_it_once_built(tpa, monkeypatch):
    tpa.is_running = True
    assert registry.running()["trend_pa"] is True
    monkeypatch.setattr(service, "_instance", None)
    assert "trend_pa" not in registry.running()
    assert "bounce" in registry.running(), "the wire slots are always reported"
