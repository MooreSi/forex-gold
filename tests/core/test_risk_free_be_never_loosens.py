"""A channel's "move SL to break even" must never LOOSEN a stop.

Live 2026-10-02, Gold Diggers VIP, ticket 2126206660 (demo):

    SL adjusted — Gold Diggers VIP
    Trade 47788eff (ticket 2126206660): 4183.51 → 4182.51
    Source: Logic Keywords (RISK FREE/BE lexicon)

A BUY filled at 4182.51 on the "Test" EA template, whose own breakeven is
`be_mode=entry_buffer, be_buffer_pts=1.0`: at TP1 it moved the stop to
4183.51, one point of profit locked. The channel then posted "TP2 HITT
+30pips / Move SL to Break Even for risk free", the RISK FREE/BE lexicon
matched, and the trigger moved the stop to bare entry -- DOWN, on a BUY,
handing the locked point back.

The owner's decision (2026-10-02): **keep the better stop.** The instruction
is "make it risk free", and a stop already at or past entry has satisfied it.
The trigger leaves it, logs why, and still claims the message.

**This is the BE trigger only.** An explicit "adjust SL to X" (learned rule,
AI fallback) still goes wherever the channel says, wider included -- Enable SL
Parsing is on and the owner wants Telegram stops honoured. The last test pins
that so the guard cannot quietly migrate into `apply_sl_adjustment`.

Nothing here reaches a broker: the bridge is the shared `tests._fakes`
double, which records `modify_order` calls and returns a canned success.
"""
import asyncio
import time

import pytest

from backend.src.db import database as db
from backend.src.services.telegram import keywords as lk
from backend.src.services.telegram import keyword_triggers as trig
from backend.src.services.trading import ai_signal_fallback as af
from tests._fakes import _FakeBridge

_CH = "Gold Diggers VIP"
_RS = {"lk_enable_risk_free_be_parsing": 1, "lk_enable_sl_parsing": 1}


def _open(direction, entry, sl, trade_id="t1"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,"
            "entry_high,stop_loss,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("sig-" + trade_id, _CH, direction, entry, entry, sl, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id,signal_id,direction,entry_low,"
            "entry_high,entry_price,lot_size,remaining_lots,stop_loss,status,open_time,"
            "tg_source,mt5_ticket) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, "sig-" + trade_id, direction, entry, entry, entry, 0.10, 0.10,
             sl, "open", time.time(), _CH, 2126206660),
        )


def _sl(trade_id="t1"):
    with db.db() as conn:
        return conn.execute(
            "SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?",
            (trade_id,)).fetchone()[0]


def _be(bridge, tg_id="22554"):
    return asyncio.run(trig.try_handle_risk_free_be_trigger(
        "TP2 HITT +30pips 🤑🤑\nMove SL to Break Even for risk free",
        _CH, tg_id, _RS, bridge=bridge))


class TestAStopAlreadyPastEntryIsKept:
    def test_the_live_case_buy_with_one_point_locked(self, fresh_db):
        _open("BUY", 4182.51, 4183.51)
        bridge = _FakeBridge()

        assert _be(bridge) is True
        assert _sl() == 4183.51
        assert bridge.modify_order_calls == [], "the broker was asked to loosen the stop"

    def test_sell_with_profit_locked_below_entry(self, fresh_db):
        _open("SELL", 4100.0, 4098.0)
        bridge = _FakeBridge()

        _be(bridge)

        assert _sl() == 4098.0
        assert bridge.modify_order_calls == []

    def test_the_message_is_still_claimed(self, fresh_db):
        """Skipping must not leave the message unclaimed, or the scan loop
        offers it again every second (signals README, bugs/065)."""
        _open("BUY", 4182.51, 4183.51)

        _be(_FakeBridge())

        assert lk.claim_trigger("22554", "risk_free_be") is False


class TestAStopShortOfEntryStillMovesToEntry:
    """Negative controls: the guard must not swallow the normal case."""

    def test_buy_below_entry_moves_up_to_entry(self, fresh_db):
        _open("BUY", 4182.51, 4176.0)
        bridge = _FakeBridge()

        _be(bridge)

        assert _sl() == 4182.51
        assert bridge.modify_order_calls == [{"ticket": 2126206660, "sl": 4182.51, "tp": None}]

    def test_sell_above_entry_moves_down_to_entry(self, fresh_db):
        _open("SELL", 4100.0, 4105.0)
        bridge = _FakeBridge()

        _be(bridge)

        assert _sl() == 4100.0
        assert len(bridge.modify_order_calls) == 1


class TestAnExplicitLevelIsNotGuarded:
    def test_a_learned_rule_may_still_widen_the_stop(self, fresh_db):
        """The owner wants Telegram stops honoured with SL Parsing on. "Adjust
        SL to 4178" on a BUY whose stop is at 4183.51 is a deliberate widening
        and goes through; only the BE trigger reads "risk free" as satisfied."""
        _open("BUY", 4182.51, 4183.51)
        bridge = _FakeBridge()

        asyncio.run(af.apply_sl_adjustment(
            4178.0, _CH, "tg-explicit", "learned_rule", bridge, rs=_RS))

        assert _sl() == 4178.0
        assert len(bridge.modify_order_calls) == 1
