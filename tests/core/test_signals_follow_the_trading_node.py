"""The Signals table shows the signals of the node that trades.

Owner, 2026-10-05: on the Mac the newest signal was from 29 Sep. The VPS is the
active trader and reads Telegram for real; the Mac's `vantage_signals` stopped
being written when it stopped trading. "This should show all of the signals
from the active node so I have full visibility."

Same mechanism, and the same rule, as the Fill cost and GEX cards
(tests/core/test_peer_report_over_sync.py): the Mac asks the VPS for a named
report and the VPS answers from its own database. With the VPS trading and
unreachable, the Mac says so; it never shows its own stale list in place of
the trader's, because that is the misreport being fixed.

Read-only. Nothing here places, closes or sizes anything, and no broker is
reached: the VPS side reads a fresh test database and the Mac side's link is a
recorder.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.cluster import peer_reports as pr
from backend.src.services.cluster import remote_control as rc
from backend.src.services.signals import repo as signals_repo
from backend.src.services.trading import engine_reads


def _signal(n: int) -> str:
    return signals_repo.create_signal(
        f"Chan {n}", "BUY", 4000.0 + n, 4002.0 + n, 3990.0 + n, tp1=4010.0 + n,
    )["signal_id"]


# ── the VPS side: the report itself ─────────────────────────────────────────

class TestTheReport:
    def test_it_is_in_the_table_the_vps_looks_up(self):
        assert "signals" in pr.REPORTS

    def test_it_answers_with_the_vps_signals_newest_first(self, fresh_db):
        first, second = _signal(1), _signal(2)

        got = asyncio.run(pr.run_local("signals", {}))

        assert [r["signal_id"] for r in got["signals"]] == [second, first]

    def test_the_limit_keeps_the_newest(self, fresh_db):
        _signal(1)
        newest = _signal(2)

        got = asyncio.run(pr.run_local("signals", {"limit": 1}))

        assert [r["signal_id"] for r in got["signals"]] == [newest]

    def test_a_status_filter_is_passed_through(self, fresh_db):
        _signal(1)

        assert asyncio.run(pr.run_local("signals", {"status": "expired"}))["signals"] == []
        assert len(asyncio.run(pr.run_local("signals", {"status": "pending"}))["signals"]) == 1

    @pytest.mark.parametrize("args", [
        {"limit": 0}, {"limit": 100_000}, {"limit": "5"}, {"limit": True},
        {"status": 7}, {"status": "x" * 200}, {"days": 3},
    ])
    def test_arguments_it_does_not_take_are_refused(self, fresh_db, args):
        with pytest.raises(ValueError):
            asyncio.run(pr.run_local("signals", args))


# ── the Mac side: which node the table reads ────────────────────────────────

class _Link:
    def __init__(self, reply=None, raises=None):
        self.reply, self.raises = reply, raises
        self.asked: list[tuple] = []

    async def request_peer_report(self, name, args, **kw):
        self.asked.append((name, args))
        if self.raises:
            raise self.raises
        return self.reply


class _Engine:
    def __init__(self):
        self.asked = 0

    def get_signals(self, status=None):
        self.asked += 1
        return [{"signal_id": "mac-local-29-sep", "status": "expired"}]


@pytest.fixture
def mac_on_vps(monkeypatch):
    holder = {"link": _Link(reply={"result": {"signals": [
        {"signal_id": "vps-today", "status": "pending"},
        {"signal_id": "vps-earlier", "status": "closed"},
    ]}})}
    monkeypatch.setattr(pr, "trader_is_peer", lambda: True)
    monkeypatch.setattr(pr._client, "get_instance", lambda: holder["link"])
    return holder


class TestOnAMacThatTradesThroughTheVps:
    def test_the_table_is_the_vps_signals(self, mac_on_vps):
        eng = _Engine()

        rows = asyncio.run(engine_reads.signals(eng))

        assert [r["signal_id"] for r in rows] == ["vps-today", "vps-earlier"]
        assert eng.asked == 0

    def test_each_row_says_which_node_it_is_from(self, mac_on_vps):
        """The editor writes to THIS node's database; a VPS row is not here."""
        rows = asyncio.run(engine_reads.signals(_Engine()))

        assert {r["node"] for r in rows} == {"remote"}

    def test_the_status_filter_reaches_the_vps(self, mac_on_vps):
        asyncio.run(engine_reads.signals(_Engine(), "pending"))

        name, args = mac_on_vps["link"].asked[0]
        assert name == "signals" and args["status"] == "pending"

    def test_an_unreachable_vps_is_reported_not_replaced(self, mac_on_vps):
        mac_on_vps["link"] = _Link(raises=ConnectionError("not connected to VPS"))
        eng = _Engine()

        with pytest.raises(rc.RemoteControlFailed):
            asyncio.run(engine_reads.signals(eng))
        assert eng.asked == 0

    def test_a_vps_on_older_code_is_told_to_update(self, mac_on_vps):
        mac_on_vps["link"] = _Link(reply={"error": "unknown report 'signals'"})

        with pytest.raises(rc.RemoteControlFailed) as exc:
            asyncio.run(engine_reads.signals(_Engine()))
        assert "update" in str(exc.value).lower()


class TestANodeThatTradesItself:
    def test_it_reads_its_own_table_unchanged(self, monkeypatch):
        """Negative control: the VPS, or a Mac trading locally, is untouched."""
        monkeypatch.setattr(pr, "trader_is_peer", lambda: False)
        eng = _Engine()

        rows = asyncio.run(engine_reads.signals(eng))

        assert rows == [{"signal_id": "mac-local-29-sep", "status": "expired"}]
        assert eng.asked == 1

