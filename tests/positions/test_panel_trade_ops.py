"""The Telegram panel's trade operations — money moved from a button press.

These are the buttons that place a market order, move a channel to breakeven,
nudge a stop and close positions. Three properties matter more than the rest,
and each has a precedent elsewhere in this codebase:

  * **Record only what the broker accepted.** Both stop-loss writes here happen
    after `modify_order` has come back clean. Recording a stop the broker
    refused is stage3/040's bug in a different place: every screen and every
    risk figure then describes a stop that is not on the order.
  * **A push that lands at or past current price is refused**, not sent. It is
    not a tighter stop — the broker may reject it, or fill it as an instant
    close of the position.
  * **One failure must not abort the batch.** "Close all" that stops at the
    first error leaves the operator believing the rest closed too.

No broker and no database: the bridge and the repo are recorded.
"""
from __future__ import annotations

import pytest

from backend.src.services.positions import _panel_trade_ops as ops
from backend.src.services.positions import panel_repo

pytestmark = pytest.mark.asyncio

CHAN = {"name": "Gold VIP", "slug": "gold-vip", "template": None}


def _trade(**over):
    t = {"trade_id": "abc123def4", "mt5_ticket": 555001, "entry_price": 2400.0,
         "direction": "BUY", "lot_size": 0.01}
    t.update(over)
    return t


class _Bridge:
    def __init__(self, modify=None, positions=None):
        self._modify = modify if modify is not None else {"success": True}
        self._positions = positions or []
        self.modified: list = []

    async def modify_order(self, ticket, sl, tp):
        self.modified.append((ticket, sl, tp))
        if isinstance(self._modify, Exception):
            raise self._modify
        return self._modify

    async def get_positions(self):
        return self._positions


class _Ctx:
    def __init__(self, bridge, closes=None):
        self._bridge = bridge
        self._closes = closes or {}
        self.closed: list = []

    async def close_trade(self, trade_id, reason):
        self.closed.append((trade_id, reason))
        out = self._closes.get(trade_id, {"net_pnl": 10.0, "close_price": 2410.0})
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def recorded(monkeypatch):
    calls: list = []
    monkeypatch.setattr(panel_repo, "record_stop_loss",
                        lambda tid, sl: calls.append((tid, sl)))
    return calls


@pytest.fixture
def channel(monkeypatch):
    box = {"chan": CHAN, "trades": [_trade()]}
    monkeypatch.setattr(ops, "_channel", lambda slug: box["chan"])
    monkeypatch.setattr(ops, "_channel_open_trades", lambda c: box["trades"])
    return box


class TestRiskFreeRecordsOnlyWhatTheBrokerAccepted:

    async def test_an_accepted_move_is_recorded(self, channel, recorded):
        ctx = _Ctx(_Bridge(modify={"success": True}))

        screen = await ops._risk_free("gold-vip", ctx)

        assert ctx._bridge.modified == [(555001, 2400.0, None)]
        assert recorded == [("abc123def4", 2400.0)]
        assert "1 position(s)" in screen.text

    async def test_a_REFUSED_move_is_not_recorded(self, channel, recorded):
        """stage3/040's shape. Recording a stop the broker refused leaves every
        screen describing a stop that is not on the order."""
        ctx = _Ctx(_Bridge(modify={"error": "Invalid stops"}))

        screen = await ops._risk_free("gold-vip", ctx)

        assert recorded == [], "recorded a stop the broker refused"
        assert "1 skipped" in screen.text

    async def test_an_EXCEPTION_is_not_recorded_either(self, channel, recorded):
        ctx = _Ctx(_Bridge(modify=OSError("bridge gone")))

        await ops._risk_free("gold-vip", ctx)

        assert recorded == []

    async def test_one_failure_does_not_abandon_the_rest(self, channel,
                                                         recorded):
        """A channel with several positions must not stop at the first bad
        one — the others are still exposed."""
        channel["trades"] = [_trade(trade_id="t1", mt5_ticket=1),
                             _trade(trade_id="t2", mt5_ticket=2),
                             _trade(trade_id="t3", mt5_ticket=3)]
        results = [{"error": "Invalid stops"}, {"success": True},
                   {"success": True}]

        class _Flaky(_Bridge):
            async def modify_order(self, ticket, sl, tp):
                self.modified.append((ticket, sl, tp))
                return results[len(self.modified) - 1]

        await ops._risk_free("gold-vip", _Ctx(_Flaky()))

        assert [t for t, _ in recorded] == ["t2", "t3"]

    async def test_an_unfilled_template_leg_is_skipped_not_modified(
            self, channel, recorded):
        """A staged leg has no ticket and no entry — there is nothing at the
        broker to move, and a modify on ticket 0 is meaningless."""
        channel["trades"] = [_trade(mt5_ticket=0, entry_price=0)]
        ctx = _Ctx(_Bridge())

        screen = await ops._risk_free("gold-vip", ctx)

        assert ctx._bridge.modified == []
        assert recorded == []
        assert "0 position(s)" in screen.text and "1 skipped" in screen.text

    async def test_an_unknown_channel_does_nothing(self, channel, recorded):
        channel["chan"] = None
        ctx = _Ctx(_Bridge())

        screen = await ops._risk_free("nope", ctx)

        assert ctx._bridge.modified == []
        assert screen.mode == "noop"


class TestPushSlRefusesAStopPastThePrice:
    """Not a tighter stop. The broker may reject it — or fill it as an instant
    close of the position."""

    @pytest.fixture
    def row(self, monkeypatch):
        r = _trade()
        monkeypatch.setattr(panel_repo, "open_trade_by_prefix", lambda p: r)
        monkeypatch.setattr(ops, "_trade_push_sl_pips", lambda row: 10.0)
        return r

    def _pos(self, sl, price, ticket=555001):
        return [{"ticket": ticket, "sl": sl, "current_price": price}]

    async def test_a_buy_push_that_reaches_the_price_is_refused(self, row,
                                                                recorded):
        # SL 2399, +10 pips lands at 2400, price is 2400 → at the price.
        bridge = _Bridge(positions=self._pos(sl=2399.0, price=2400.0))

        screen = await ops._push_sl_one("abc123", _Ctx(bridge))

        assert bridge.modified == [], "sent a stop at the current price"
        assert recorded == []
        assert "refused" in screen.toast

    async def test_a_SELL_push_past_the_price_is_refused_too(self, row,
                                                             recorded):
        row["direction"] = "SELL"
        bridge = _Bridge(positions=self._pos(sl=2401.0, price=2400.0))

        screen = await ops._push_sl_one("abc123", _Ctx(bridge))

        assert bridge.modified == []
        assert "refused" in screen.toast

    async def test_a_position_with_no_stop_at_all_is_refused(self, row,
                                                             recorded):
        """Pushing from 0 would compute a stop near zero and send it."""
        bridge = _Bridge(positions=self._pos(sl=0.0, price=2400.0))

        screen = await ops._push_sl_one("abc123", _Ctx(bridge))

        assert bridge.modified == []
        assert "refused" in screen.toast

    async def test_a_valid_push_is_sent_and_recorded(self, row, recorded):
        """Positive control: a stop still well below the price moves."""
        bridge = _Bridge(positions=self._pos(sl=2390.0, price=2410.0))

        await ops._push_sl_one("abc123", _Ctx(bridge))

        assert len(bridge.modified) == 1
        assert bridge.modified[0][1] > 2390.0
        assert recorded and recorded[0][1] == bridge.modified[0][1]

    async def test_a_refused_modify_is_not_recorded(self, row, recorded):
        bridge = _Bridge(modify={"error": "Invalid stops"},
                         positions=self._pos(sl=2390.0, price=2410.0))

        screen = await ops._push_sl_one("abc123", _Ctx(bridge))

        assert recorded == []
        assert "Push SL failed" in screen.text

    async def test_a_position_the_broker_does_not_report_is_refused(
            self, row, recorded):
        """Computing a push from a stale database row rather than the live
        position is how a stop ends up somewhere nobody intended."""
        bridge = _Bridge(positions=[])

        screen = await ops._push_sl_one("abc123", _Ctx(bridge))

        assert bridge.modified == []
        assert screen.mode == "noop"


class TestClosingManyIsNotAbandonedOnTheFirstFailure:

    async def test_every_trade_is_attempted(self):
        trades = [_trade(trade_id="t1"), _trade(trade_id="t2"),
                  _trade(trade_id="t3")]
        ctx = _Ctx(_Bridge(), closes={"t2": RuntimeError("broker refused")})

        text = await ops._close_many(trades, ctx, "Gold VIP")

        assert [t for t, _ in ctx.closed] == ["t1", "t2", "t3"], (
            "stopped at the first failure -- the operator is told the rest "
            "closed when they are still open"
        )
        assert "Failed" in text

    async def test_the_total_counts_only_what_actually_closed(self):
        trades = [_trade(trade_id="t1"), _trade(trade_id="t2")]
        ctx = _Ctx(_Bridge(), closes={
            "t1": {"net_pnl": 25.0, "close_price": 2410.0},
            "t2": RuntimeError("broker refused"),
        })

        text = await ops._close_many(trades, ctx, "Gold VIP")

        assert "Total P&L: +$25.00" in text
