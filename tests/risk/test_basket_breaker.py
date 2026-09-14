"""A basket scores once, on its net. A leg inside one never scores alone.

`docs/todo/bugs/041`. Global Harvest banks a COMBINED total and closes every
position on the symbol, deliberately including legs that are individually
losing — that is what banking a combined total means. The circuit breaker then
counted each leg separately, so a **profitable** basket pushed the account
toward a halt:

    20:19:15  harvest closes 4 positions: +39.3 +38.4 +32.2 -31.1  (net +$78.10)
    21:16:17  stop loss -$48.80
    21:16:17  [CB] Circuit breaker triggered — live trading blocked for 15 min.

Threshold 3, only two real losses after the basket, so the counter stood at 1
when it finished: the -$31.10 leg had been counted as a consecutive loss. It
happened again on 2026-09-10, more starkly — an **eighty-cent** leg inside a
+$107.19 basket, and fifty minutes later a fifteen-minute halt.

The owner's answer, 2026-09-11, option **B**: *"a harvest shouldn't trip or
count towards the breaker"*, and a winning basket still clears the counter.

| the basket | the counter |
|---|---|
| net >= 0 | **reset to 0** |
| net < 0 | **untouched** — no increment, no reset |

and no leg ever scores on its own, whichever way it went.

**Nothing here is live until the EA marks its harvest closes.** An unregistered
ticket scores exactly as it does today, which is what the first class asserts.
"""
from __future__ import annotations

import pytest

from backend.src.services.risk import basket_breaker
from backend.src.services.risk import circuit_breaker_repo as cb


@pytest.fixture(autouse=True)
def breaker_on(fresh_db):
    cb.update_risk_settings({
        "circuit_breaker_enabled": 1,
        "circuit_breaker_losses": 3,
        "circuit_breaker_consec_losses": 0,
        "circuit_breaker_active_until": 0.0,
    })
    basket_breaker.forget_all()
    yield
    basket_breaker.forget_all()


def _consec() -> int:
    return cb.get_circuit_breaker_state()["consec_losses"]


class TestAnOrdinaryCloseIsUnchanged:
    """The path every close takes today, and the one that must not move until
    an EA that marks its baskets is actually deployed."""

    def test_a_loss_still_increments(self):
        basket_breaker.score_close(mt5_ticket=111, won=False)

        assert _consec() == 1

    def test_a_win_still_resets(self):
        basket_breaker.score_close(mt5_ticket=111, won=False)
        basket_breaker.score_close(mt5_ticket=222, won=True)

        assert _consec() == 0

    def test_it_still_reports_when_it_trips(self):
        for t in (1, 2):
            state = basket_breaker.score_close(mt5_ticket=t, won=False)
            assert not state["just_triggered"]

        state = basket_breaker.score_close(mt5_ticket=3, won=False)

        assert state["just_triggered"] is True
        assert state["is_active"] is True

    def test_a_ticket_from_an_unknown_basket_is_an_ordinary_close(self):
        basket_breaker.register("b1", net_pnl=50.0, tickets=[777])

        basket_breaker.score_close(mt5_ticket=888, won=False)

        assert _consec() == 1


class TestALosingLegInsideAWinningBasket:
    def test_the_losing_leg_does_not_count(self):
        """The whole bug. -$31.10 inside a +$78.10 basket used to leave the
        counter at 1."""
        basket_breaker.register("b1", net_pnl=78.10, tickets=[1, 2, 3, 4])

        basket_breaker.score_close(mt5_ticket=4, won=False)

        assert _consec() == 0

    def test_a_winning_basket_clears_a_streak(self):
        """Option B rather than pure invisibility: the basket banked money, so
        it resets, which is what a consecutive-LOSS counter means."""
        basket_breaker.score_close(mt5_ticket=900, won=False)
        basket_breaker.score_close(mt5_ticket=901, won=False)
        basket_breaker.register("b1", net_pnl=107.19, tickets=[1, 2])

        basket_breaker.score_close(mt5_ticket=1, won=False)

        assert _consec() == 0

    def test_the_basket_scores_once_however_many_legs_arrive(self):
        """Five legs, one verdict. Scoring per leg is the bug in a different
        costume."""
        basket_breaker.score_close(mt5_ticket=900, won=False)
        basket_breaker.score_close(mt5_ticket=901, won=False)
        basket_breaker.register("b1", net_pnl=-5.0, tickets=[1, 2, 3, 4, 5])

        for t in (1, 2, 3, 4, 5):
            basket_breaker.score_close(mt5_ticket=t, won=False)

        assert _consec() == 2


class TestALosingBasket:
    def test_it_leaves_the_counter_exactly_where_it_was(self):
        """Untouched, not incremented and not reset. The basket was an
        account-level action, not a per-trade outcome."""
        basket_breaker.score_close(mt5_ticket=900, won=False)
        basket_breaker.register("b1", net_pnl=-40.0, tickets=[1, 2])

        basket_breaker.score_close(mt5_ticket=1, won=False)
        basket_breaker.score_close(mt5_ticket=2, won=True)

        assert _consec() == 1

    def test_a_losing_basket_cannot_trip_the_breaker(self):
        basket_breaker.score_close(mt5_ticket=900, won=False)
        basket_breaker.score_close(mt5_ticket=901, won=False)
        basket_breaker.register("b1", net_pnl=-40.0, tickets=[1, 2, 3])

        for t in (1, 2, 3):
            state = basket_breaker.score_close(mt5_ticket=t, won=False)
            assert not state["just_triggered"]

        assert cb.get_circuit_breaker_state()["is_active"] is False


class TestScoringOnceIsObservable:
    def test_a_late_leg_does_not_re_clear_a_streak_that_started_after_it(self):
        """Why "once" is more than tidiness.

        The basket clears the counter. A genuine loss then closes and the
        counter is 1. If a remaining leg of the same basket arrives after that
        — the legs are separate closes and nothing guarantees they are
        contiguous — scoring it again would wipe a streak the basket had
        nothing to do with."""
        basket_breaker.register("b1", net_pnl=107.19, tickets=[1, 2])
        basket_breaker.score_close(mt5_ticket=1, won=False)

        basket_breaker.score_close(mt5_ticket=900, won=False)   # a real loss
        basket_breaker.score_close(mt5_ticket=2, won=False)     # the late leg

        assert _consec() == 1

    def test_a_second_announcement_cannot_change_the_verdict(self):
        """The EA announcing the same basket twice must not let the second
        number decide. First registration wins; anything else means a resend
        with a stale or partial total could flip a losing basket into a
        counter-clearing one."""
        basket_breaker.score_close(mt5_ticket=900, won=False)
        basket_breaker.register("b1", net_pnl=-10.0, tickets=[1])
        basket_breaker.register("b1", net_pnl=+500.0, tickets=[1])

        basket_breaker.score_close(mt5_ticket=1, won=False)

        assert _consec() == 1


class TestTheBoundary:
    def test_a_basket_that_nets_exactly_zero_resets(self):
        """`>= 0`. A scratch basket is not a losing one, and the spec says so:
        net profit >= 0 resets."""
        basket_breaker.score_close(mt5_ticket=900, won=False)
        basket_breaker.register("b1", net_pnl=0.0, tickets=[1])

        basket_breaker.score_close(mt5_ticket=1, won=False)

        assert _consec() == 0


class TestTheRegistryDoesNotLeak:
    def test_a_basket_is_forgotten_once_every_leg_has_arrived(self):
        """Tickets are reused by the broker over time, and a registry that kept
        them forever would eventually make an ordinary close invisible."""
        basket_breaker.register("b1", net_pnl=10.0, tickets=[1, 2])
        basket_breaker.score_close(mt5_ticket=1, won=True)
        basket_breaker.score_close(mt5_ticket=2, won=True)

        basket_breaker.score_close(mt5_ticket=1, won=False)

        assert _consec() == 1

    def test_registering_the_same_basket_twice_does_not_double_score(self):
        basket_breaker.score_close(mt5_ticket=900, won=False)
        basket_breaker.register("b1", net_pnl=-10.0, tickets=[1])
        basket_breaker.register("b1", net_pnl=-10.0, tickets=[1])

        basket_breaker.score_close(mt5_ticket=1, won=False)

        assert _consec() == 1
