"""A DB close the broker refused is a phantom close (stage3/040).

`check_profit_close_target` calls the broker to close, then records the close
in the database **unconditionally**:

    if mt5_ticket:
        try:
            mt5_res = await bridge.close_position(int(mt5_ticket))
            if mt5_res.get("success"):
                close_price = float(mt5_res.get("close_price", cur))
        except Exception as _e:
            log.warning("Profit-close MT5 error: %s", _e)
    result = await record_close(...)          # <- runs either way

Two ways through: the broker returns success=False, which was never checked at
all, or it raises, which was caught and warned. Both then record the trade
closed while MT5 still holds the position.

That is the worst shape of wrong. The app believes the trade is finished, so
nothing manages it any more -- no stop watched, no target, no harvest -- while
a real position stays open and moving. The P&L booked is fictional too.

The rule: record a close only when the broker CONFIRMED one. Otherwise log
loudly, notify, and leave the row open for reconciliation (030) to settle.

The frozen close path is not edited by any of this -- only its caller.
"""
from __future__ import annotations

import logging

import pytest

from backend.src.services.positions import monitor_loop
from backend.src.services.trading.close_trade import CloseTradeContext




class _Tick:
    bid = 4050.0
    ask = 4050.5


class _Bridge:
    def __init__(self, result=None, raises=None):
        self._result = result
        self._raises = raises
        self.close_calls = 0

    async def close_position(self, ticket):
        self.close_calls += 1
        if self._raises:
            raise self._raises
        return self._result


def _trade(ticket=111):
    return {"trade_id": "t-1", "direction": "BUY", "entry_price": 4000.0,
            "remaining_lots": 0.1, "realised_pnl": 0.0, "mt5_ticket": ticket,
            "lot_size": 0.1}


@pytest.fixture
def recorded(monkeypatch):
    """Records whether the DB close was written, without writing it."""
    calls: list = []

    async def _fake_record_close(trade_id, price, reason, ctx):
        calls.append({"trade_id": trade_id, "price": price, "reason": reason})
        return {"trade_id": trade_id, "net_pnl": 5.0}

    monkeypatch.setattr(monitor_loop, "record_close", _fake_record_close)
    return calls


@pytest.fixture
def ctx(monkeypatch):
    """A context whose async side-effects do nothing."""
    class _Ctx:
        bridge = None
        starting_balance = 1000.0

        async def schedule_profit_sync(self, *a, **kw):
            return None

        async def background_close_commentary(self, *a, **kw):
            return None

    return _Ctx()


@pytest.mark.asyncio
class TestAConfirmedCloseStillRecords:
    """The negative control. If this failed, the tests below would pass by
    never closing anything at all."""

    async def test_a_successful_broker_close_records_once(self, recorded, ctx):
        bridge = _Bridge(result={"success": True, "close_price": 4051.0})

        closed = await monitor_loop.check_profit_close_target(
            _trade(), _Tick(), profit_close_usd=1.0, bridge=bridge, ctx=ctx)

        assert closed is True
        assert len(recorded) == 1
        assert recorded[0]["price"] == 4051.0

    async def test_a_trade_with_no_ticket_still_records(self, recorded, ctx):
        """A simulated trade has no broker side to confirm, so the DB close is
        the whole event. Gating it on a broker result would strand every
        non-live trade permanently open."""
        bridge = _Bridge(result={"success": True})

        closed = await monitor_loop.check_profit_close_target(
            _trade(ticket=None), _Tick(), 1.0, bridge, ctx)

        assert closed is True
        assert len(recorded) == 1
        assert bridge.close_calls == 0


@pytest.mark.asyncio
class TestARefusedCloseRecordsNOTHING:
    async def test_success_false_records_no_close(self, recorded, ctx, caplog):
        """success=False was never checked. The DB said closed, MT5 said open,
        and nothing managed the position from then on."""
        bridge = _Bridge(result={"success": False, "error": "Invalid stops"})

        with caplog.at_level(logging.ERROR):
            closed = await monitor_loop.check_profit_close_target(
                _trade(), _Tick(), 1.0, bridge, ctx)

        assert recorded == [], "a DB close was recorded for a refused broker close"
        assert closed is False, "the caller was told the trade closed"

    async def test_it_says_so_at_ERROR(self, recorded, ctx, caplog):
        """It was a warning for the exception case and silent for the
        success=False case. A phantom close deserves an error."""
        bridge = _Bridge(result={"success": False, "error": "Invalid stops"})

        with caplog.at_level(logging.ERROR):
            await monitor_loop.check_profit_close_target(
                _trade(), _Tick(), 1.0, bridge, ctx)

        text = " ".join(r.getMessage() for r in caplog.records)
        assert "Invalid stops" in text

    async def test_an_EXCEPTION_records_no_close_either(self, recorded, ctx):
        bridge = _Bridge(raises=RuntimeError("connection reset"))

        closed = await monitor_loop.check_profit_close_target(
            _trade(), _Tick(), 1.0, bridge, ctx)

        assert recorded == []
        assert closed is False

    async def test_a_None_result_records_no_close(self, recorded, ctx):
        """A bridge that returns None has not confirmed anything -- the same
        distinction stage3/020 turns on for opens."""
        bridge = _Bridge(result=None)

        closed = await monitor_loop.check_profit_close_target(
            _trade(), _Tick(), 1.0, bridge, ctx)

        assert recorded == []
        assert closed is False

    async def test_the_position_is_left_for_reconciliation(self, recorded, ctx):
        """Leaving it open is the point: 030's pass sees a DB row the broker
        still has, reports it, and the trade stays managed in the meantime."""
        bridge = _Bridge(result={"success": False, "error": "market closed"})

        await monitor_loop.check_profit_close_target(
            _trade(), _Tick(), 1.0, bridge, ctx)

        assert recorded == []


@pytest.mark.asyncio
class TestTheTriggerConditionIsUnchanged:
    """Only what happens AFTER the broker responds may change. When a close is
    attempted must be byte-identical."""

    async def test_below_the_target_nothing_is_attempted(self, recorded, ctx):
        bridge = _Bridge(result={"success": True})

        closed = await monitor_loop.check_profit_close_target(
            _trade(), _Tick(), profit_close_usd=10_000.0, bridge=bridge, ctx=ctx)

        assert closed is False
        assert bridge.close_calls == 0
        assert recorded == []

    async def test_a_zero_target_is_disabled(self, recorded, ctx):
        bridge = _Bridge(result={"success": True})

        assert await monitor_loop.check_profit_close_target(
            _trade(), _Tick(), 0.0, bridge, ctx) is False
        assert bridge.close_calls == 0

    async def test_a_zero_entry_placeholder_is_skipped(self, recorded, ctx):
        """Unrealised P&L from a zero entry is contract-value-sized and would
        trip any target. Unchanged guard, asserted so the fix cannot disturb
        it."""
        bridge = _Bridge(result={"success": True})
        t = _trade()
        t["entry_price"] = 0.0

        assert await monitor_loop.check_profit_close_target(
            t, _Tick(), 1.0, bridge, ctx) is False
        assert bridge.close_calls == 0


class TestTheAlertCannotTakeDownTheMonitorLoop:
    """`_report_close_refused` runs inside the monitor cycle. If its alert
    raised, position management would stop -- which is far worse than the
    missed alert it was trying to send."""

    def test_a_raising_alert_is_swallowed(self, monkeypatch):
        """Exercised, not read. Mutation showed the wrapper was never actually
        entered by the other tests: they run under a live event loop, so
        create_task always succeeded and nothing ever threw."""
        from backend.src.services.telegram import alerts as telegram_alerts

        def _boom(*a, **kw):
            raise RuntimeError("telegram formatting blew up")

        monkeypatch.setattr(telegram_alerts, "send_message", _boom)

        monitor_loop._report_close_refused(
            {"trade_id": "t-1", "mt5_ticket": 111}, "broker said no")

    def test_it_still_logs_the_error_when_the_alert_fails(self, monkeypatch, caplog):
        """The log line is the floor. It happens before the alert is even
        attempted, so a broken alert never costs the record."""
        from backend.src.services.telegram import alerts as telegram_alerts

        monkeypatch.setattr(telegram_alerts, "send_message",
                            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("nope")))

        with caplog.at_level(logging.ERROR):
            monitor_loop._report_close_refused(
                {"trade_id": "t-1", "mt5_ticket": 111}, "broker said no")

        text = " ".join(r.getMessage() for r in caplog.records)
        assert "NOT closed" in text
        assert "broker said no" in text
