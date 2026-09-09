"""Cancelling a signal must withdraw the broker-side order it placed.

**Found running demo 7 on the demo account, 2026-09-09.** A limit order was
placed from Trading > Limit Order, then cancelled from Pending Signals. The UI
said "Signal cancelled". The order was still resting at the broker, and the
database proved it:

    vantage_pending_orders  trade_id=a401553e  signal_id=4fd3a43f
                            ea_ticket=1973407311  status='working'
    vantage_signals         signal_id=4fd3a43f  status='cancelled'

`runtime.cancel_signal` called the signals repo and nothing else. The repo does
one UPDATE on `vantage_signals`. Nothing ever asked the EA to withdraw the
order.

**Why this is a money problem, not tidiness.** Since bugs/026 a resting order
consumes a trade slot, so a "cancelled" order goes on blocking a slot until it
expires -- four hours, by the EA's own `expiresMin=240`. It can also still
FILL. The operator believes they have cancelled a trade and may be opened into
one anyway.

**And there was no other way out.** No controller and no UI exposed a cancel
for a working pending order, so once placed, an order could not be withdrawn
from the app at all.

Fixed on the owner's explicit permission to touch money-path issues.
"""
from __future__ import annotations

import pytest

from backend.src.services.signals import cancellation


class _EA:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    async def cancel_pending_order(self, trade_id, ticket, reason):
        self.calls.append((trade_id, ticket, reason))
        return self.ok


@pytest.fixture
def wiring(monkeypatch):
    state = {"cancelled": [], "orders": [], "resolved": [], "ea": _EA()}

    monkeypatch.setattr(cancellation._repo, "cancel_signal",
                        lambda sid: state["cancelled"].append(sid))
    monkeypatch.setattr(cancellation, "_working_orders_for",
                        lambda sid: [o for o in state["orders"]
                                     if o.get("signal_id") == sid])
    monkeypatch.setattr(cancellation, "_mark_cancelled",
                        lambda tid, sid: state["resolved"].append(tid))
    monkeypatch.setattr(cancellation, "_get_ea", lambda: state["ea"])
    return state


def _order(**kw):
    base = {"trade_id": "t-1", "signal_id": "s-1", "ea_ticket": 1973407311}
    base.update(kw)
    return base


class TestTheOrderIsActuallyWithdrawn:
    def test_a_working_order_is_cancelled_at_the_broker(self, wiring):
        wiring["orders"].append(_order())

        cancellation.cancel_signal("s-1")

        assert wiring["ea"].calls, "the EA was never asked to withdraw anything"
        trade_id, ticket, _reason = wiring["ea"].calls[0]
        assert (trade_id, ticket) == ("t-1", 1973407311)

    def test_the_signal_is_still_cancelled(self, wiring):
        """The original behaviour must not be lost."""
        wiring["orders"].append(_order())

        cancellation.cancel_signal("s-1")

        assert wiring["cancelled"] == ["s-1"]

    def test_the_row_is_marked_so_the_slot_is_freed(self, wiring):
        """A row left 'working' keeps consuming a trade slot (bugs/026)."""
        wiring["orders"].append(_order())

        cancellation.cancel_signal("s-1")

        assert wiring["resolved"] == ["t-1"]


class TestItOnlyTouchesTheRightOrder:
    def test_another_signals_order_is_left_alone(self, wiring):
        wiring["orders"].append(_order(signal_id="someone-else", trade_id="t-9"))

        cancellation.cancel_signal("s-1")

        assert wiring["ea"].calls == []

    def test_an_order_with_no_broker_ticket_is_not_guessed_at(self, wiring):
        """Guessing a ticket would cancel someone else's order -- the same rule
        resting_revalidation already follows."""
        wiring["orders"].append(_order(ea_ticket=0))

        cancellation.cancel_signal("s-1")

        assert wiring["ea"].calls == []

    def test_a_signal_with_no_order_still_cancels_cleanly(self, wiring):
        cancellation.cancel_signal("s-1")

        assert wiring["cancelled"] == ["s-1"]
        assert wiring["ea"].calls == []


class TestItNeverBlocksTheCancel:
    def test_no_ea_still_cancels_the_signal(self, wiring, monkeypatch):
        """The user asked to cancel. A missing EA must not turn that into an
        error that leaves the signal live as well as the order."""
        monkeypatch.setattr(cancellation, "_get_ea", lambda: None)
        wiring["orders"].append(_order())

        cancellation.cancel_signal("s-1")

        assert wiring["cancelled"] == ["s-1"]

    def test_a_throwing_ea_still_cancels_the_signal(self, wiring):
        class _Boom:
            async def cancel_pending_order(self, *a):
                raise RuntimeError("socket gone")
        wiring["ea"] = _Boom()
        wiring["orders"].append(_order())

        cancellation.cancel_signal("s-1")

        assert wiring["cancelled"] == ["s-1"]

    def test_a_refused_withdrawal_does_not_mark_the_row_resolved(self, wiring):
        """If the broker did not withdraw it, the row must keep saying
        'working' -- it is still consuming a slot and still able to fill."""
        wiring["ea"] = _EA(ok=False)
        wiring["orders"].append(_order())

        cancellation.cancel_signal("s-1")

        assert wiring["resolved"] == []


class TestTheUIActuallyReachesThisService:
    """The Pending Signals card calls `engine.cancel_signal`. If runtime goes
    back to the repo directly, every test above passes and the bug returns."""

    def test_runtime_calls_the_service_not_the_repo(self):
        """Asserted on the BOUND FUNCTION, not on source text.

        runtime binds the service at import as `_cancel_signal_svc`, so a
        source grep of the method body would show only that name and prove
        nothing about where it points.
        """
        from backend.src import runtime

        assert runtime._cancel_signal_svc is cancellation.cancel_signal, (
            "runtime.cancel_signal no longer points at the cancellation "
            "service, so a cancelled signal leaves its order resting again"
        )

    def test_the_repo_shortcut_is_gone(self):
        """The repo's cancel_signal must not be reachable from runtime under
        any alias -- that shortcut is exactly the bug."""
        from backend.src import runtime
        from backend.src.services.signals import repo as signals_repo

        aliases = [k for k, v in vars(runtime).items()
                   if v is signals_repo.cancel_signal]

        assert aliases == [], f"runtime still binds the repo directly: {aliases}"


class TestTheFilterItself:
    """`_working_orders_for` is stubbed in every test above, so the real
    filtering was never exercised -- and a mutant returning EVERY working
    order, not just this signal's, survived the whole file.

    These drive the real function and stub only the repo beneath it.
    """

    @pytest.fixture
    def repo_rows(self, monkeypatch):
        rows: list = []
        from backend.src.services.broker import repo as broker_repo
        monkeypatch.setattr(broker_repo, "fetch_working_pending_orders",
                            lambda: list(rows))
        return rows

    def test_it_returns_only_the_matching_signals_orders(self, repo_rows):
        repo_rows.extend([
            _order(signal_id="s-1", trade_id="mine"),
            _order(signal_id="s-2", trade_id="theirs"),
        ])

        got = cancellation._working_orders_for("s-1")

        assert [r["trade_id"] for r in got] == ["mine"]

    def test_no_match_returns_nothing(self, repo_rows):
        repo_rows.append(_order(signal_id="s-2"))

        assert cancellation._working_orders_for("s-1") == []

    def test_a_repo_that_throws_yields_nothing_rather_than_raising(
            self, monkeypatch):
        """This runs inside a user-initiated cancel. A read failure must not
        turn the cancel into an error."""
        from backend.src.services.broker import repo as broker_repo

        def _boom():
            raise RuntimeError("db gone")
        monkeypatch.setattr(broker_repo, "fetch_working_pending_orders", _boom)

        assert cancellation._working_orders_for("s-1") == []
