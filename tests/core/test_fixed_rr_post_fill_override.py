"""Fixed R:R corrects its stop and target once the real fill is known.

Both levels are fixed distances from the ACTUAL fill, but MT5 needs a valid
stop on the order before the fill exists, so the trade is opened against a
proxy computed off the zone mid and rewritten afterwards. Unlike every other
strategy the target is pushed to the broker too, because this one is
deliberately unmanaged after open -- MT5 has to hold both sides itself.

None of that was covered. The whole block sat in open_from_signal.py's
uncovered lines, including the arm that matters most: modify_order signals a
broker rejection by RETURNING an error dict, never by raising. The source
comment points at the live incident where ignoring that recorded a stop the
broker had refused and cost a full-width loss.

Nothing here reaches MT5. The bridge is a fake and the DB write is captured.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.trading import open_from_signal as ofs
from tests._fakes import _FakeBridge
from backend.src.utils.models import STRATEGY_FIXED_RR

SL_PT = 4.0
TP_PT = 6.0
# Deliberately NOT the zone mid (3999.0-4001.0 -> 4000.0). If these two
# coincide, a test asserting the levels come from the fill passes even when
# the code uses the mid -- which is exactly what happened on the first pass.
FILL = 4003.50


def _bridge(reply=None, raises=False):
    """The shared fake, not a 51st local one -- tests/refactor/
    test_fixture_dedup.py caps those and was right to stop me."""
    return _FakeBridge(modify_order_result=reply, modify_order_raises=raises)


@pytest.fixture
def harness(monkeypatch):
    """Drive open_trade_from_signal as far as the Fixed R:R branch."""
    written = {}

    async def _resolve(bridge, signal_id, **kw):
        return {
            "sig": {"direction": "BUY", "entry_low": 3999.0, "entry_high": 4001.0,
                    "source_name": "Test"},
            "strategy": STRATEGY_FIXED_RR,
            "lot_size": 0.10,
            "stop_loss_to_use": 3990.0,
            "tick": object(),
        }

    async def _open_trade(bridge, **kw):
        return {"trade_id": "t-fixedrr-1", "entry_price": FILL, "mt5_ticket": 555}

    monkeypatch.setattr(ofs, "resolve_open_trade_params", _resolve)
    monkeypatch.setattr(ofs, "open_trade", _open_trade)
    monkeypatch.setattr(ofs.trade_repo, "claim_signal_activation", lambda sid: 1)
    monkeypatch.setattr(ofs.trade_repo, "apply_fixed_rr_levels",
                        lambda tid, sl, tp: written.update(trade_id=tid, sl=sl, tp=tp))
    monkeypatch.setattr(ofs, "get_strategy_params",
                        lambda strat: {"sl_pt": SL_PT, "tp_pt": TP_PT})
    return written


def _run(bridge):
    return asyncio.run(ofs.open_trade_from_signal(bridge, "sig-1"))


class TestLevelsComeFromTheFill:
    def test_the_stop_and_target_are_measured_from_the_actual_fill(self, harness):
        """The zone mid is 4000.0 and the fill is 4003.50, so a stop of 3999.50
        can only have come from the fill. Using the mid would give 3996.00."""
        _run(_bridge())
        assert harness["sl"] == pytest.approx(FILL - SL_PT)
        assert harness["tp"] == pytest.approx(FILL + TP_PT)

    def test_a_sell_inverts_both(self, harness, monkeypatch):
        """A sign error here puts the stop on the profitable side and the
        target where the stop should be."""
        async def _resolve_sell(bridge, signal_id, **kw):
            return {
                "sig": {"direction": "SELL", "entry_low": 3999.0,
                        "entry_high": 4001.0, "source_name": "Test"},
                "strategy": STRATEGY_FIXED_RR, "lot_size": 0.10,
                "stop_loss_to_use": 4010.0, "tick": object(),
            }
        monkeypatch.setattr(ofs, "resolve_open_trade_params", _resolve_sell)
        _run(_bridge())
        assert harness["sl"] == pytest.approx(FILL + SL_PT)
        assert harness["tp"] == pytest.approx(FILL - TP_PT)

    def test_the_written_levels_are_the_ones_pushed_to_the_broker(self, harness):
        """The DB row and MT5 must agree. They are computed once and used
        twice; if that ever splits, the app reports levels the broker is not
        holding."""
        bridge = _bridge()
        _run(bridge)
        assert bridge.modify_order_calls == [{"ticket": 555, "sl": harness["sl"], "tp": harness["tp"]}]


class TestTheTargetReachesTheBroker:
    def test_a_take_profit_is_sent_not_just_a_stop(self, harness):
        """The distinguishing behaviour of this strategy. Nothing manages it
        after open, so a missing broker TP means the target never fires."""
        bridge = _bridge()
        _run(bridge)
        assert bridge.modify_order_calls[0]["tp"] is not None

    def test_no_ticket_means_no_modify_attempt(self, harness, monkeypatch):
        """A trade opened without an MT5 ticket has nothing to modify."""
        async def _no_ticket(bridge, **kw):
            return {"trade_id": "t-1", "entry_price": FILL, "mt5_ticket": None}
        monkeypatch.setattr(ofs, "open_trade", _no_ticket)
        bridge = _bridge()
        _run(bridge)
        assert bridge.modify_order_calls == []
        assert harness["sl"] == pytest.approx(FILL - SL_PT), "the row is still corrected"


class TestBrokerRejection:
    """The arm with a live incident behind it. modify_order reports refusal by
    RETURNING an error dict -- it does not raise -- so a caller that only wraps
    it in try/except records levels the broker never accepted."""

    def test_a_refusal_is_noticed_and_logged_as_an_error(self, harness, caplog):
        import logging
        bridge = _bridge(reply={"success": False, "error": "invalid stops"})
        with caplog.at_level(logging.ERROR):
            _run(bridge)
        joined = caplog.text
        assert "REJECTED" in joined, "a refused SL/TP sync must be loud"
        assert "invalid stops" in joined, "the broker's reason must be reported"

    def test_a_refusal_does_not_take_the_open_down_with_it(self, harness):
        """The trade exists and is running -- on the proxy levels. Raising here
        would strand a live position behind an exception."""
        result = _run(_bridge(reply={"success": False, "error": "no"}))
        assert result["trade_id"] == "t-fixedrr-1"

    def test_a_bridge_that_raises_is_survived_too(self, harness):
        result = _run(_bridge(raises=True))
        assert result["trade_id"] == "t-fixedrr-1"
