"""A live-executed signal must record how far it actually travelled.

data-inspect/003. `re_signals.mfe_pts`/`mae_pts` are the only record of how far
a signal ran each way, and `_manage_ref_ladder_signal`'s own comment says why
they exist: "without it, any change to stop width or target distance is a
guess".

**They were never recorded for live trades.** `record_excursion` is called only
from `_manage_triggered_signal`, and `_manage_triggered_signal`'s docstring
says live-executed signals "are routed to _reconcile_live_signal instead, never
here". Measured on the owner's data 2026-09-08: of 745 executed signals only 52
carry an MFE, all of them before 2026-08-28, and **zero** of the 165 executed in
September do.

That is the instrumentation needed to answer the two questions that actually
decide whether this engine makes money -- why losses average -1.161R against a
1.0R stop, and why wins are closed at 0.642R -- so it is fixed before either is
touched. Changing an exit rule on the 35 usable samples that exist today would
be the guess the comment warns about.

This records only. It moves no stop, closes nothing, and changes no decision.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.src.services.reversal_engine import reversal_engine_manage as rem


TICKET = 1677417630


class _Bridge:
    def __init__(self, open_tickets=(TICKET,)):
        self.open_tickets = list(open_tickets)

    async def get_positions(self):
        return [{"ticket": t} for t in self.open_tickets]


class _Engine(rem._ManagementMixin):
    def __init__(self, bridge):
        self._bridge = bridge
        self._live_missing_streak = {}

    def _notify_refresh(self):
        pass

    async def _template_leg_tickets(self, sig, ticket):
        return {int(ticket)}


def _sig(direction="BUY", trigger=4000.0):
    return {"id": 7, "signal_ref": "RE-X", "mt5_ticket": TICKET,
            "direction": direction, "entry_low": trigger - 1,
            "entry_high": trigger + 1, "trigger_price": trigger}


def _tick(price):
    return SimpleNamespace(mid=price, bid=price, ask=price)


@pytest.fixture
def recorded(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(rem.re_db, "record_excursion",
                        lambda sig_id, fav, adv: calls.append((sig_id, fav, adv)))
    return calls


def _run(sig, tick, open_tickets=(TICKET,)):
    eng = _Engine(_Bridge(open_tickets))
    asyncio.run(eng._reconcile_live_signal(sig, tick))
    return eng


class TestItRecordsWhileTheTradeIsOpen:
    def test_a_buy_in_profit_widens_the_favourable_watermark(self, recorded):
        _run(_sig("BUY", 4000.0), _tick(4006.0))

        assert recorded, "nothing was recorded for a live open trade"
        sig_id, fav, adv = recorded[0]
        assert sig_id == 7
        assert fav == pytest.approx(6.0)
        assert adv == pytest.approx(-6.0)

    def test_a_buy_in_loss_widens_the_adverse_watermark(self, recorded):
        _run(_sig("BUY", 4000.0), _tick(3995.5))

        _, fav, adv = recorded[0]
        assert fav == pytest.approx(-4.5)
        assert adv == pytest.approx(4.5)

    def test_a_sell_is_the_other_way_round(self, recorded):
        """Direction inverted, or every SELL records its loss as a profit."""
        _run(_sig("SELL", 4000.0), _tick(3993.0))

        _, fav, adv = recorded[0]
        assert fav == pytest.approx(7.0)
        assert adv == pytest.approx(-7.0)

    def test_it_measures_from_the_fill_not_the_zone_midpoint(self, recorded):
        """trigger_price is the realistic fill. Measuring from the zone mid
        would misreport every signal whose fill was not dead centre."""
        sig = _sig("BUY", 4000.0)
        sig["entry_low"], sig["entry_high"] = 3990.0, 4010.0   # mid = 4000 too
        sig["trigger_price"] = 4004.0                          # but filled here

        _run(sig, _tick(4009.0))

        _, fav, _adv = recorded[0]
        assert fav == pytest.approx(5.0), "measured from the zone, not the fill"


class TestItDoesNotDisturbAnythingElse:
    def test_the_still_open_signal_is_left_alone(self, recorded):
        """Negative control: recording must not end the reconcile early or
        mark the signal closed. The streak reset is the existing behaviour."""
        eng = _run(_sig(), _tick(4006.0))

        assert eng._live_missing_streak == {}

    def test_no_tick_records_nothing_and_does_not_raise(self, recorded):
        """The caller fetches a tick and it can be None. A missing price is
        not a reason to stop reconciling a live trade."""
        _run(_sig(), None)

        assert recorded == []

    def test_a_recorder_that_throws_does_not_break_reconciliation(self, monkeypatch):
        """Same guarantee the virtual path already gives. This is
        measurement; it must never cost a live trade its management."""
        def _boom(*_a, **_k):
            raise RuntimeError("db gone")
        monkeypatch.setattr(rem.re_db, "record_excursion", _boom)

        _run(_sig(), _tick(4006.0))   # must not raise
