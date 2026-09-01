"""Shifting a breached signal's geometry to the price it is actually entered at.

Owner decisions, 2026-09-01 (docs/simon-handover/009):

  * **A** — a breached zone still DISCARDS by default. Nothing changes for
    anyone who has not switched Entry Realignment on.
  * **And realignment on the market path if the option is selected** — the
    setting existed only in the limit-order path, so the same situation was
    handled two different ways depending on which route a signal took.

What realignment does: the market has moved through the zone toward the stop
before any entry existed, so entering at market would leave a smaller stop than
the channel specified. Instead the stop and every target move by the same
distance, and the trade keeps the shape it was sent with — just at a worse
price.

The real case Simon watched on 2026-08-28:

    SELL  entry 4537.00-4539.00  SL 4544.00  TP1 4535.00
    price 4540.45 -- above the zone, 3.55 below the stop

Entering flat there gives 3.55 of stop instead of 5.00. Realigned, the stop
becomes 4545.45 and TP1 4536.45: 5.00 of stop and 5.00 to TP1, exactly as sent.

The safety property that matters most is the last class here — a realigned stop
must stay on the correct side of the entry. One on the wrong side is not a wide
stop, it is an immediate close.
"""
from __future__ import annotations

import pytest

from backend.src.services.trading import entry_realignment as er


class TestTheCaseThatPromptedIt:
    """SELL 4537-4539, SL 4544, price 4540.45."""

    def _real(self):
        return er.realign_for_breach(
            direction="SELL", entry_low=4537.0, entry_high=4539.0,
            live_px=4540.45, stop_loss=4544.0,
            tps={1: 4535.0, 2: 4533.0, 3: 4531.0},
        )

    def test_the_stop_keeps_its_original_distance(self):
        out = self._real()

        assert out.stop_loss == pytest.approx(4545.45)
        assert out.stop_loss - 4540.45 == pytest.approx(4544.0 - 4539.0)

    def test_every_target_moves_by_the_same_amount(self):
        out = self._real()

        assert out.tps == {1: 4536.45, 2: 4534.45, 3: 4532.45}

    def test_the_delta_is_measured_from_the_breached_edge(self):
        out = self._real()

        assert out.delta == pytest.approx(4540.45 - 4539.0)


class TestTheBuySide:
    """Mirror image: price falls BELOW the zone, toward a stop underneath."""

    def _real(self, live_px=4530.0):
        return er.realign_for_breach(
            direction="BUY", entry_low=4537.0, entry_high=4539.0,
            live_px=live_px, stop_loss=4530.0,
            tps={1: 4545.0, 2: 4550.0},
        )

    def test_the_stop_moves_down_with_the_price(self):
        out = self._real()

        assert out.delta == pytest.approx(4530.0 - 4537.0)
        assert out.stop_loss == pytest.approx(4523.0)

    def test_the_targets_move_down_too(self):
        out = self._real()

        assert out.tps == {1: 4538.0, 2: 4543.0}

    def test_the_geometry_is_identical_to_the_original(self):
        out = self._real()

        original_risk = 4537.0 - 4530.0
        original_reward = 4545.0 - 4537.0

        assert 4530.0 - out.stop_loss == pytest.approx(original_risk)
        assert out.tps[1] - 4530.0 == pytest.approx(original_reward)


class TestItOnlyAppliesToAnActualBreach:
    """Called on a price inside or beyond the zone in the OTHER direction, it
    must decline rather than shift a trade that does not need shifting."""

    @pytest.mark.parametrize("direction,live_px", [
        ("BUY", 4538.0),    # inside the zone
        ("SELL", 4538.0),   # inside the zone
        ("BUY", 4545.0),    # above a buy zone -- ran away, not breached
        ("SELL", 4530.0),   # below a sell zone -- ran away, not breached
    ])
    def test_it_returns_nothing(self, direction, live_px):
        assert er.realign_for_breach(
            direction=direction, entry_low=4537.0, entry_high=4539.0,
            live_px=live_px, stop_loss=4530.0 if direction == "BUY" else 4544.0,
            tps={1: 4545.0},
        ) is None

    def test_exactly_on_the_edge_is_not_a_breach(self):
        """`price_in_entry_range` treats the edge as in-zone; realignment has
        to agree, or the two disagree about the same price."""
        assert er.realign_for_breach(
            direction="SELL", entry_low=4537.0, entry_high=4539.0,
            live_px=4539.0, stop_loss=4544.0, tps={1: 4535.0},
        ) is None


class TestAStopOnTheWrongSideIsRefused:
    """Not a wide stop -- an immediate close. If the arithmetic ever produces
    one, no trade is better than that trade."""

    def test_a_buy_whose_realigned_stop_lands_above_entry_is_refused(self):
        """Only reachable from a signal whose stop was already on the wrong
        side, but this is the last check before an order."""
        assert er.realign_for_breach(
            direction="BUY", entry_low=4537.0, entry_high=4539.0,
            live_px=4530.0, stop_loss=4540.0,      # stop ABOVE a buy zone
            tps={1: 4545.0},
        ) is None

    def test_a_sell_whose_realigned_stop_lands_below_entry_is_refused(self):
        assert er.realign_for_breach(
            direction="SELL", entry_low=4537.0, entry_high=4539.0,
            live_px=4540.45, stop_loss=4536.0,     # stop BELOW a sell zone
            tps={1: 4535.0},
        ) is None

    def test_a_zero_distance_stop_is_refused(self):
        assert er.realign_for_breach(
            direction="SELL", entry_low=4537.0, entry_high=4539.0,
            live_px=4540.45, stop_loss=4539.0,
            tps={1: 4535.0},
        ) is None


class TestTheDetails:
    def test_empty_targets_are_left_out_rather_than_shifted_from_zero(self):
        """A `None` TP shifted by the delta would become a real price near
        zero, and the EA would take it as a target."""
        out = er.realign_for_breach(
            direction="SELL", entry_low=4537.0, entry_high=4539.0,
            live_px=4540.45, stop_loss=4544.0,
            tps={1: 4535.0, 2: None, 3: 0.0},
        )

        assert set(out.tps) == {1}

    def test_prices_are_rounded_to_two_places(self):
        out = er.realign_for_breach(
            direction="SELL", entry_low=4537.0, entry_high=4539.0,
            live_px=4540.457, stop_loss=4544.0, tps={1: 4535.0},
        )

        assert out.stop_loss == round(out.stop_loss, 2)
        assert all(v == round(v, 2) for v in out.tps.values())

    def test_it_reports_the_price_it_would_enter_at(self):
        out = er.realign_for_breach(
            direction="SELL", entry_low=4537.0, entry_high=4539.0,
            live_px=4540.45, stop_loss=4544.0, tps={1: 4535.0},
        )

        assert out.entry_px == pytest.approx(4540.45)
