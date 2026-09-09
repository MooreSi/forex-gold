"""A pending Telegram signal must be re-checked when price finally reaches it.

Asked by the owner 2026-09-09: *"if there is a pending/resting order either in
the app or on the ea does it re-evaluate the order before executing the trade
in case the market has changed since the signal was created?"*

**Partly, and unevenly.** There are three waiting states, and they were not
treated alike:

| waiting state | re-checked at execution |
|---|---|
| Reversal Engine signal awaiting trigger | schedule, news, fresh `ml_prob`, bias, fill delay |
| EA resting order (`vantage_pending_orders`) | bias -- swept and cancelled (reversal-engine/050) |
| **Telegram signal awaiting its zone** | **pre-trade filters only, and those are bypassed for templates** |

`reversal_engine_live_execute` re-asks the schedule and the news blackout at
the moment of the fill, deliberately. The pending Telegram watcher asked
neither, so a signal could sit for an hour and open inside a news blackout or
outside the trading schedule.

The bias gate already covers this path: activation runs through
`open_trade_from_signal` -> `resolve_open_trade_params`, where that gate sits
outside the template exemption. This adds the ones the Reversal Engine has and
this route did not, using the same functions rather than new copies.
"""
from __future__ import annotations

import inspect

import pytest

from backend.src.services.signals import pending_activation as pa

_TRIPLE_DOUBLE = '"' * 3
_TRIPLE_SINGLE = "'" * 3


def _activation_body() -> str:
    """The activation function's own source, comments and docstrings stripped.

    Everything here is asserted on the CALL, inside THIS function -- never on
    the module. Both precautions were forced by mutation on this very file:

    * a module-wide search matches the `from ... import check_trading_schedule`
      line, so deleting the guard entirely still passed -- and because the
      import sits at the top of the file, it satisfied the ordering assertions
      too;
    * the prose names the same functions.

    Searching for `name(` rather than `name` is what separates the call from
    the import, since the import carries no parenthesis.
    """
    out = []
    for line in inspect.getsource(pa.try_activate_pending_signals).splitlines():
        stripped = line.strip()
        if (stripped.startswith("#")
                or stripped.startswith(_TRIPLE_DOUBLE)
                or stripped.startswith(_TRIPLE_SINGLE)):
            continue
        out.append(line.split("#")[0] if "#" in line else line)
    return "\n".join(out)


GUARDS = ["check_news_blackout(", "check_trading_schedule(", "fill_too_soon("]


class TestItAsksTheSameQuestionsTheReversalEngineAsks:
    """Not new rules -- the same ones, on the route that lacked them."""

    @pytest.mark.parametrize("guard", GUARDS)
    def test_the_guard_is_actually_called(self, guard):
        assert guard in _activation_body()


class TestTheGuardsRunBeforeTheOrder:
    """A guard consulted after the order is placed is decoration."""

    @pytest.mark.parametrize("guard", GUARDS)
    def test_the_guard_precedes_the_order_call(self, guard):
        body = _activation_body()

        assert body.index(guard) < body.index("open_trade_from_signal("), (
            f"{guard} runs after the order is placed"
        )

    @pytest.mark.parametrize("guard", GUARDS)
    def test_a_failed_guard_skips_the_signal(self, guard):
        """It must `continue` to the next queued signal, not fall through.
        A guard that logs and carries on is worse than none: it reports a
        refusal that did not happen."""
        body = _activation_body()
        after = body[body.index(guard):]

        assert "continue" in after[:600], f"{guard} does not skip the signal"


class TestTheGuardsAreTheSharedOnes:
    """One definition each, so this route cannot drift from the Reversal
    Engine's answer to the same question."""

    def test_no_hand_rolled_news_or_schedule_logic(self):
        src = inspect.getsource(pa)
        for smell in ("blackout_minutes", "datetime.now().hour", "schedule_window"):
            assert smell not in src, (
                f"pending_activation appears to re-implement {smell} instead of "
                f"calling the shared check"
            )

    def test_the_fill_delay_comes_from_the_governor(self):
        assert "_gov.fill_too_soon(" in _activation_body()
