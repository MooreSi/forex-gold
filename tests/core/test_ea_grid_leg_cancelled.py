"""When every leg of an EA Template grid expires without filling.

This is the handler behind a live incident. On 2026-08-03 two single-leg grids
each had their only resting leg expire unfilled, and each sat in Active Trades
for five hours at a fabricated ~$16,132 unrealised P&L — the
`(current_price - 0) * lots` arithmetic that every $0-entry placeholder row
produces. The row said a trade was open; no broker position had ever existed.

The fix hangs on one number, `grid_legs_total`, and on telling three states
apart that all look alike from here:

    total = N     N legs were placed. Close only once N have cancelled.
    total = 0     confirmed: this grid placed nothing.
    total = None  UNKNOWN -- the row came from an ack-timeout placeholder,
                  where the EA may genuinely have placed legs Python never
                  heard about. Never close on this.

The code's own comment records that `if total and ...` treated 0 and None
identically, which is the bug in miniature: a confirmed zero and an unknown are
not the same, exactly as elsewhere in this codebase.

Nothing here reaches a broker: `record_close` is the frozen close path and is
recorded rather than run.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.broker.ea_bridge import _events

pytestmark = pytest.mark.asyncio

TRADE = "abc123def4"


async def _cancel(node, leg_id, reason="expired"):
    """Run the handler, then let its `create_task` alerts actually run.

    The alerts are dispatched fire-and-forget so a slow Telegram call cannot
    stall event handling. Asserting on them without yielding first tests
    nothing -- the task has not started.
    """
    await node._on_grid_leg_cancelled(leg_id, reason)
    await asyncio.sleep(0)
    await asyncio.sleep(0)


def _row(**over):
    row = {"trade_id": TRADE, "status": "open", "mt5_ticket": 0,
           "direction": "BUY", "tg_source": "Gold VIP",
           "grid_legs_total": 2}
    row.update(over)
    return row


class _Bridge:
    pass


class _Node(_events.EventsMixin):
    """A bridge stripped to what this handler touches."""

    def __init__(self, row, cancelled_after=1):
        self._row = row
        self._cancelled_after = cancelled_after
        self._engine = type("E", (), {"_bridge": _Bridge()})()
        self.closed: list = []
        self.fetches = 0
        self.fetched_ids: list = []

    async def _fetch_trade(self, trade_id):
        self.fetches += 1
        self.fetched_ids.append(trade_id)
        return self._row

    async def _incr_grid_leg_cancelled(self, trade_id):
        return self._cancelled_after

    async def _close_dead_grid_placeholder(self, row, reason):
        self.closed.append((row["trade_id"], reason))


@pytest.fixture
def alerts(monkeypatch):
    sent: list = []

    async def _send(text, tid=None, kind=None):
        sent.append(kind or text)
    monkeypatch.setattr(_events.telegram_alerts, "send_message", _send)
    return sent


class TestNothingHappensWhenThereIsNothingToDo:

    async def test_no_row_at_all(self, alerts):
        node = _Node(row=None)

        await _cancel(node, f"{TRADE}-g1")

        assert node.closed == []
        assert alerts == []

    async def test_a_row_another_leg_already_filled(self, alerts):
        """`mt5_ticket != 0` means a sibling filled and promoted the row. A
        losing leg cancelling afterwards is expected and harmless."""
        node = _Node(_row(mt5_ticket=558899))

        await _cancel(node, f"{TRADE}-g2")

        assert node.closed == []
        assert alerts == []

    async def test_a_row_that_is_no_longer_open(self, alerts):
        node = _Node(_row(status="closed"))

        await _cancel(node, f"{TRADE}-g1")

        assert node.closed == []
        assert alerts == []


class TestTheThreeStatesOfGridLegsTotal:

    async def test_all_legs_cancelled_closes_the_placeholder(self, alerts):
        """The 2026-08-03 case, once the count is known."""
        node = _Node(_row(grid_legs_total=2), cancelled_after=2)

        await _cancel(node, f"{TRADE}-g2")

        assert node.closed == [(TRADE, "expired")]

    async def test_SOME_legs_cancelled_leaves_it_open_and_says_so(self, alerts):
        """Other legs may still be resting. Closing here would abandon a grid
        that is still live."""
        node = _Node(_row(grid_legs_total=3), cancelled_after=1)

        await _cancel(node, f"{TRADE}-g1")

        assert node.closed == []
        assert alerts == ["template_grid_leg_cancelled"]

    async def test_a_CONFIRMED_zero_closes_it(self, alerts):
        """`total == 0` is "this grid placed nothing", which is information.
        The old `if total and ...` read it as falsy and left the row open
        forever."""
        node = _Node(_row(grid_legs_total=0), cancelled_after=1)

        await _cancel(node, f"{TRADE}-g1")

        assert node.closed == [(TRADE, "expired")]

    async def test_an_UNKNOWN_total_never_closes(self, alerts):
        """None comes from an ack-timeout placeholder, where the EA may have
        placed legs Python never heard about. Closing on that could book a
        trade shut while a real position is open at the broker."""
        node = _Node(_row(grid_legs_total=None), cancelled_after=99)

        await _cancel(node, f"{TRADE}-g1")

        assert node.closed == []
        assert alerts == ["template_grid_leg_cancelled"], (
            "an unknown leg count must surface and wait, not close and not "
            "stay silent"
        )

    async def test_zero_and_None_are_NOT_treated_alike(self, alerts):
        """The distinction, asserted directly. Both are falsy; only one is an
        answer."""
        confirmed = _Node(_row(grid_legs_total=0), cancelled_after=1)
        unknown = _Node(_row(grid_legs_total=None), cancelled_after=1)

        await _cancel(confirmed, f"{TRADE}-g1")
        await _cancel(unknown, f"{TRADE}-g1")

        assert confirmed.closed and not unknown.closed


class TestTheRowIsRE_CHECKED_BeforeClosing:
    """Between the increment and the close, another leg can fill and promote
    the row. Closing then would shut a trade that has a live position."""

    async def test_a_row_promoted_mid_flight_is_not_closed(self, alerts):
        class _Racing(_Node):
            async def _fetch_trade(self, trade_id):
                self.fetches += 1
                self.fetched_ids.append(trade_id)
                # First read: still an unfilled placeholder. Second read (after
                # the increment): a sibling leg has filled.
                return (_row() if self.fetches == 1
                        else _row(mt5_ticket=558899))

        node = _Racing(_row(), cancelled_after=2)

        await _cancel(node, f"{TRADE}-g2")

        assert node.fetches == 2, "the row was not re-read after the increment"
        assert node.closed == [], "a promoted trade was closed as a dead placeholder"

    async def test_a_row_closed_mid_flight_is_not_closed_again(self, alerts):
        class _Racing(_Node):
            async def _fetch_trade(self, trade_id):
                self.fetches += 1
                self.fetched_ids.append(trade_id)
                return _row() if self.fetches == 1 else _row(status="closed")

        node = _Racing(_row(), cancelled_after=2)

        await _cancel(node, f"{TRADE}-g2")

        assert node.closed == []


class TestTheLegIdIsResolvedToItsParent:
    async def test_the_row_is_looked_up_by_the_PARENT_id(self, alerts):
        """The EA reports a LEG id; the database row is keyed by the parent.
        Looking it up by the leg id finds nothing, and the placeholder is left
        open forever -- which is the shape of the original incident, reached a
        different way."""
        node = _Node(_row(), cancelled_after=2)

        await _cancel(node, f"{TRADE}-g7")

        assert node.fetched_ids and all(i == TRADE for i in node.fetched_ids), (
            f"looked the row up by {node.fetched_ids!r} rather than the parent "
            f"{TRADE!r}"
        )
        assert node.closed == [(TRADE, "expired")]

    async def test_an_id_with_no_leg_suffix_is_left_alone(self, alerts):
        """Control: a plain trade id must pass through unchanged rather than
        being truncated by an over-eager split."""
        node = _Node(_row(), cancelled_after=2)

        await _cancel(node, TRADE)

        assert node.fetched_ids == [TRADE, TRADE]
        assert node.closed == [(TRADE, "expired")]
