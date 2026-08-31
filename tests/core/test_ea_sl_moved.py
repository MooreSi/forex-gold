"""The EA reporting that a stop has moved.

Two things depend on this landing correctly. The database's `stop_loss` is what
every risk figure, every reconciliation pass and every screen reads — if a
breakeven lock or a trail is not recorded, the app believes the trade is still
risking its original stop. And the alert is how the operator learns it
happened.

The subtle one is `owns`. An EA Template grid has several legs, all reporting
against the same parent row. Only the leg that actually IS this row's position
may write its stop; a sibling leg moving its own stop must be reported and
nothing more. Writing a sibling's stop onto the parent would misreport the risk
on a position that never moved.

`tp_cleared_num` comes from the EA because only it knows which `tp[]` index
fired. It used to be hardcoded to 0 here, which displayed as "TP0 cleared" on
every EA-reported breakeven — confirmed live on ticket 1556988985.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.broker.ea_bridge import _events

pytestmark = pytest.mark.asyncio

TRADE = {"trade_id": "abc123", "direction": "BUY", "tg_source": "Gold VIP",
         "stop_loss": 2392.0}


class _Node(_events.EventsMixin):
    def __init__(self, resolved):
        self._resolved = resolved

    async def _resolve_leg_event(self, msg, kind):
        return self._resolved


@pytest.fixture
def writes(monkeypatch):
    """Capture stop-loss writes without touching a database."""
    calls: list = []
    from backend.src.services.broker import repo as broker_repo
    from backend.src.db import database as db_module

    monkeypatch.setattr(broker_repo, "set_stop_loss_be",
                        lambda tid, sl: calls.append((tid, sl)))

    async def _to_db_thread(fn, *a, **kw):
        return fn(*a, **kw)
    monkeypatch.setattr(db_module, "to_db_thread", _to_db_thread)
    return calls


@pytest.fixture
def alerts(monkeypatch):
    sent: list = []
    from backend.src.services.telegram import alerts as tg

    async def _send(text, tid=None, kind=None):
        sent.append(kind)
    monkeypatch.setattr(tg, "send_message", _send)
    monkeypatch.setattr(tg, "fmt_sl_moved",
                        lambda trade, n, sl: f"sl_moved tp={n} sl={sl}")
    monkeypatch.setattr(tg, "fmt_template_leg_note",
                        lambda trade, label, title, lines: "leg note")
    return sent


async def _handle(node, **msg):
    await node._on_sl_moved({"trade_id": "abc123", "new_sl": 2400.5, **msg})
    await asyncio.sleep(0)
    await asyncio.sleep(0)


class TestOnlyThePositionsOWNLegMayWriteItsStop:

    async def test_the_owning_leg_writes_the_new_stop(self, writes, alerts):
        node = _Node((TRADE, "abc123", "anchor", True))

        await _handle(node, new_sl=2400.5)

        assert writes == [("abc123", 2400.5)]
        assert alerts == ["sl_moved_ea"]

    async def test_a_SIBLING_leg_writes_NOTHING(self, writes, alerts):
        """It is reported, because the operator should see it, but the parent
        row's stop is not this leg's stop."""
        node = _Node((TRADE, "abc123", "grid 2", False))

        await _handle(node, new_sl=2400.5)

        assert writes == [], (
            "a sibling leg's stop was written onto the parent row -- every "
            "risk figure now describes a move that did not happen to it"
        )
        assert alerts == ["sl_moved_ea_sibling_leg"]

    async def test_an_unresolvable_event_does_nothing_at_all(self, writes,
                                                             alerts):
        node = _Node((None, "abc123", "", False))

        await _handle(node)

        assert writes == []
        assert alerts == []


class TestTheTpNumberComesFromTheEa:
    """Only the EA knows which tp[] index fired."""

    async def test_the_reported_number_is_passed_through(self, writes, alerts,
                                                         monkeypatch):
        seen: list = []
        from backend.src.services.telegram import alerts as tg
        monkeypatch.setattr(tg, "fmt_sl_moved",
                            lambda trade, n, sl: seen.append(n) or "x")
        node = _Node((TRADE, "abc123", "anchor", True))

        await _handle(node, tp_cleared_num=3)

        assert seen == [3], "the EA said TP3 fired and the alert said otherwise"

    async def test_a_continuous_trail_reports_zero(self, writes, alerts,
                                                   monkeypatch):
        """0 means "not tied to a specific TP". That is a real value, not a
        missing one."""
        seen: list = []
        from backend.src.services.telegram import alerts as tg
        monkeypatch.setattr(tg, "fmt_sl_moved",
                            lambda trade, n, sl: seen.append(n) or "x")
        node = _Node((TRADE, "abc123", "anchor", True))

        await _handle(node, tp_cleared_num=0)

        assert seen == [0]

    async def test_a_missing_number_does_not_raise(self, writes, alerts):
        node = _Node((TRADE, "abc123", "anchor", True))

        await _handle(node)

        assert writes == [("abc123", 2400.5)]


class TestAFailureHereDoesNotStopEventHandling:
    async def test_a_database_error_is_logged_not_raised(self, alerts,
                                                         monkeypatch, caplog):
        """This runs inside the EA message loop. Raising takes down every
        subsequent event -- fills, closes, leg cancellations -- for one failed
        stop write."""
        from backend.src.db import database as db_module

        async def _boom(fn, *a, **kw):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(db_module, "to_db_thread", _boom)
        node = _Node((TRADE, "abc123", "anchor", True))

        with caplog.at_level("WARNING"):
            await _handle(node)          # must not raise

        assert any("sl_moved handling failed" in r.getMessage()
                   for r in caplog.records)
