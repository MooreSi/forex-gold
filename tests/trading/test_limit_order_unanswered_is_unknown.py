"""A limit order the EA did not answer is UNKNOWN, not failed.

2026-09-28, owner's demo account: Gold Diggers tg_id=31099 was sent to the EA
as a BUY LIMIT @ 4127.00. The ack did not arrive inside 5s, the Limit Runner
reported "Limit order failed" and wrote nothing -- but the EA had placed it.
It filled at 18:06 (ticket 2104195879), the fill arrived as
"pending_order_filled for unknown trade_id", and the position ran to TP with
no row, no management and no record of the $19.20.

docs/system/rules/20-trading-safety.md, "found / absent / UNKNOWN": a broker
that could not be asked has not said no. So a timed-out send is recorded --
the order row as 'unknown', the signal parked as 'unknown' -- which means:

  * a late fill finds its row and becomes a managed trade;
  * nothing re-sends it: restore and re-arm read 'working'/'withdrawn' only,
    and the pending-signal scheduler reads 'pending' only.

A send that demonstrably never left (EA refused, socket write failed) is
still a plain failure: the broker is known not to have it.

Nothing here reaches a broker. The EA is a fake that raises or returns.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from backend.src.db import database as db
from backend.src.services.broker import repo as broker_repo
from backend.src.services.signals import repo as signals_repo
from backend.src.services.trading import limit_order_signal as los


class _FakeEA:
    def __init__(self, exc):
        self._exc = exc
        self.trade_ids: list[str] = []

    def is_ea_healthy(self):
        return True

    async def place_pending_order(self, trade_id, *a, **kw):
        self.trade_ids.append(trade_id)
        raise self._exc


async def _balance():
    return 1000.0


def _lot_size(entry, sl, balance, risk_pct):
    return 0.02


def _parsed():
    return {
        "direction": "BUY", "entry_low": 4122.0, "entry_high": 4127.0,
        "stop_loss": 4118.0, "tp1": 4131.0, "tp2": 4135.0, "tp3": None,
        "tp4": None, "tp5": None, "tp6": None, "tp7": None, "tp8": None,
        "tp_open": False,
    }


def _insert_tg_row(tg_id):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_tg_signals (tg_message_id,group_id,raw_text,parsed_at,status) "
            "VALUES (?,?,?,?,?)",
            (tg_id, "g1", "raw", 0.0, "new"),
        )


async def _run(ea, tg_id="31099"):
    _insert_tg_row(tg_id)
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=ea):
        return await los.handle_limit_order_signal(
            _parsed(), tg_id, "GOLD DIGGERS INSTITUTIONAL", "GOLD DIGGERS INSTITUTIONAL",
            {"risk_per_trade_pct": 0.5, "strategy_lot_size": 0},
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
        )


def _all_pending_orders():
    with db.db() as conn:
        return [db.row_to_dict(r) for r in conn.execute(
            "SELECT * FROM vantage_pending_orders").fetchall()]


class TestTimeoutIsRecorded:
    @pytest.mark.asyncio
    async def test_the_order_row_exists_as_unknown(self, fresh_db):
        ea = _FakeEA(asyncio.TimeoutError())
        await _run(ea)
        row = broker_repo.fetch_pending_order(ea.trade_ids[0])
        assert row, "a late pending_order_filled must find this trade_id"
        assert row["status"] == "unknown"
        assert row["price"] == 4127.0
        assert row["direction"] == "BUY"

    @pytest.mark.asyncio
    async def test_the_signal_is_parked_not_pending(self, fresh_db):
        ea = _FakeEA(asyncio.TimeoutError())
        await _run(ea)
        row = broker_repo.fetch_pending_order(ea.trade_ids[0])
        sig = signals_repo.get_signal(row["signal_id"])
        assert sig["status"] == "unknown"

    @pytest.mark.asyncio
    async def test_the_report_does_not_say_failed(self, fresh_db):
        result = await _run(_FakeEA(asyncio.TimeoutError()))
        assert "failed" not in result["skip_reason"].lower()
        assert "unconfirmed" in result["skip_reason"].lower()


class TestNothingResendsIt:
    @pytest.mark.asyncio
    async def test_restore_does_not_push_it(self, fresh_db):
        await _run(_FakeEA(asyncio.TimeoutError()))
        assert broker_repo.fetch_working_pending_orders() == []

    @pytest.mark.asyncio
    async def test_revalidation_does_not_rearm_it(self, fresh_db):
        await _run(_FakeEA(asyncio.TimeoutError()))
        assert broker_repo.fetch_revalidatable_pending_orders() == []

    @pytest.mark.asyncio
    async def test_the_zone_watcher_does_not_activate_it(self, fresh_db):
        await _run(_FakeEA(asyncio.TimeoutError()))
        assert signals_repo.get_pending_signals_awaiting_zone_fill() == []


class TestAKnownRefusalIsStillAFailure:
    @pytest.mark.asyncio
    async def test_send_that_never_left_writes_nothing(self, fresh_db):
        result = await _run(_FakeEA(ConnectionError("EA send failed")))
        assert "failed" in result["skip_reason"].lower()
        assert _all_pending_orders() == []
