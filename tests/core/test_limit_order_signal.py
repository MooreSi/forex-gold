"""core_limit_order_signal.handle_limit_order_signal() -- places a genuine
EA pending order for a parsed "BUY/SELL [LIMITS] GOLD @ high/low AREA"
signal (STRATEGY_LIMIT_RUNNER). Covers: session/per-signal-skip gating, the
EA-unavailable skip (no Python-bridge fallback exists for this strategy),
dynamic pcts math (with and without a "TP OPEN" runner leg), the near-edge
entry price choice, and the DB rows written on a successful placement."""
import json
import os
import tempfile
from unittest.mock import patch

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_limit_order_signal as los
from forex_trader.core import core_strategy_params as sp
from forex_trader.core.models import STRATEGY_LIMIT_RUNNER


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    sp._cache.clear()
    yield db
    sp._cache.clear()
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _FakeEA:
    def __init__(self, healthy=True, ack=None, raise_exc=None):
        self._healthy = healthy
        self._ack = ack if ack is not None else {"type": "pending_order_placed", "ticket": 555}
        self._raise = raise_exc
        self.calls = []

    def is_ea_healthy(self):
        return self._healthy

    async def place_pending_order(self, trade_id, direction, price, lot_size, stop_loss,
                                  tps, pcts, be_at_pos, strategy, expire_minutes=240.0,
                                  close_full_on_last=True):
        self.calls.append(dict(
            trade_id=trade_id, direction=direction, price=price, lot_size=lot_size,
            stop_loss=stop_loss, tps=dict(tps), pcts=list(pcts), be_at_pos=be_at_pos,
            strategy=strategy, expire_minutes=expire_minutes,
            close_full_on_last=close_full_on_last,
        ))
        if self._raise:
            raise self._raise
        return self._ack


def _parsed(direction="BUY", tp_open=True, n_tps=3):
    tp_prices = {"BUY": [4151.0, 4155.0, 4160.0], "SELL": [4176.0, 4172.0, 4168.0]}[direction]
    d = {
        "direction": direction, "entry_low": 4142.0, "entry_high": 4148.0,
        "stop_loss": 4141.0 if direction == "BUY" else 4189.0,
        "tp1": None, "tp2": None, "tp3": None, "tp4": None,
        "tp5": None, "tp6": None, "tp7": None, "tp8": None,
        "tp_open": tp_open,
    }
    for i, price in enumerate(tp_prices[:n_tps], start=1):
        d[f"tp{i}"] = price
    return d


async def _balance():
    return 1000.0


def _lot_size(entry, sl, balance, risk_pct):
    return 0.10


def _rs():
    return {"risk_per_trade_pct": 0.5, "strategy_lot_size": 0}


async def _insert_tg_row(tg_id="tg1"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_tg_signals (tg_message_id,group_id,raw_text,parsed_at,status) "
            "VALUES (?,?,?,?,?)",
            (tg_id, "g1", "raw", 0.0, "new"),
        )


@pytest.mark.asyncio
async def test_skips_when_session_not_ok(fresh_db):
    result = await los.handle_limit_order_signal(
        _parsed(), "tg1", "chan", "chan", _rs(),
        sess_ok=False, per_signal_skip=False, per_signal_skip_reason="",
        skip_reason="Outside trading session",
        get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
    )
    assert result["skip_reason"] == "Outside trading session"


@pytest.mark.asyncio
async def test_skips_when_per_signal_skip(fresh_db):
    result = await los.handle_limit_order_signal(
        _parsed(), "tg1", "chan", "chan", _rs(),
        sess_ok=True, per_signal_skip=True, per_signal_skip_reason="High risk",
        skip_reason="",
        get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
    )
    assert "High risk" in result["skip_reason"]


@pytest.mark.asyncio
async def test_skips_when_ea_not_connected(fresh_db):
    with patch("forex_trader.core.ea_bridge.get_instance", return_value=None):
        result = await los.handle_limit_order_signal(
            _parsed(), "tg1", "chan", "chan", _rs(),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
        )
    assert "EA not connected" in result["skip_reason"]


@pytest.mark.asyncio
async def test_skips_when_ea_unhealthy(fresh_db):
    fake_ea = _FakeEA(healthy=False)
    with patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        result = await los.handle_limit_order_signal(
            _parsed(), "tg1", "chan", "chan", _rs(),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
        )
    assert "EA not connected" in result["skip_reason"]
    assert fake_ea.calls == []


@pytest.mark.asyncio
async def test_no_tps_is_skipped(fresh_db):
    parsed = _parsed(n_tps=0)
    fake_ea = _FakeEA()
    with patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        result = await los.handle_limit_order_signal(
            parsed, "tg1", "chan", "chan", _rs(),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
        )
    assert "no TP levels" in result["skip_reason"]
    assert fake_ea.calls == []


@pytest.mark.asyncio
async def test_buy_uses_entry_high_as_price(fresh_db):
    await _insert_tg_row("tg1")
    fake_ea = _FakeEA()
    with patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        await los.handle_limit_order_signal(
            _parsed("BUY"), "tg1", "chan", "chan", _rs(),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
        )
    assert fake_ea.calls[0]["price"] == 4148.0  # entry_high


@pytest.mark.asyncio
async def test_sell_uses_entry_low_as_price(fresh_db):
    await _insert_tg_row("tg1")
    fake_ea = _FakeEA()
    with patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        await los.handle_limit_order_signal(
            _parsed("SELL"), "tg1", "chan", "chan", _rs(),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
        )
    assert fake_ea.calls[0]["price"] == 4142.0  # entry_low


@pytest.mark.asyncio
async def test_tp_open_reserves_runner_pct_and_sets_close_full_on_last_false(fresh_db):
    await _insert_tg_row("tg1")
    fake_ea = _FakeEA()
    with patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        await los.handle_limit_order_signal(
            _parsed("BUY", tp_open=True, n_tps=3), "tg1", "chan", "chan", _rs(),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
        )
    call = fake_ea.calls[0]
    # default runner_reserve_pct=25% -> 75% split across 3 TPs = 0.25 each
    assert call["pcts"] == pytest.approx([0.25, 0.25, 0.25])
    assert call["close_full_on_last"] is False
    # default be_at_pos param = 1 (TP1) -> 0-indexed compacted position 0
    assert call["be_at_pos"] == 0


@pytest.mark.asyncio
async def test_no_tp_open_splits_evenly_and_closes_full_on_last(fresh_db):
    await _insert_tg_row("tg1")
    fake_ea = _FakeEA()
    with patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        await los.handle_limit_order_signal(
            _parsed("BUY", tp_open=False, n_tps=3), "tg1", "chan", "chan", _rs(),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
        )
    call = fake_ea.calls[0]
    assert call["pcts"] == pytest.approx([1 / 3, 1 / 3, 1 / 3])
    assert call["close_full_on_last"] is True


@pytest.mark.asyncio
async def test_be_at_pos_param_is_converted_from_1_based_to_0_indexed(fresh_db):
    await _insert_tg_row("tg1")
    sp.set_strategy_params(STRATEGY_LIMIT_RUNNER, {"be_at_pos": 2})
    fake_ea = _FakeEA()
    with patch("forex_trader.sync.client.get_instance", return_value=None), \
         patch("forex_trader.sync.server.get_instance", return_value=None), \
         patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        await los.handle_limit_order_signal(
            _parsed("BUY"), "tg1", "chan", "chan", _rs(),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
        )
    assert fake_ea.calls[0]["be_at_pos"] == 1  # "TP2" (1-based) -> compacted pos 1


@pytest.mark.asyncio
async def test_ea_rejection_is_reported_as_skip_reason(fresh_db):
    await _insert_tg_row("tg1")
    fake_ea = _FakeEA(ack={"type": "pending_order_open_failed", "error": "Invalid stops"})
    with patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        result = await los.handle_limit_order_signal(
            _parsed("BUY"), "tg1", "chan", "chan", _rs(),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
        )
    assert "Invalid stops" in result["skip_reason"]


@pytest.mark.asyncio
async def test_successful_placement_writes_db_rows(fresh_db):
    await _insert_tg_row("tg1")
    fake_ea = _FakeEA(ack={"type": "pending_order_placed", "ticket": 999})
    with patch("forex_trader.core.ea_bridge.get_instance", return_value=fake_ea):
        result = await los.handle_limit_order_signal(
            _parsed("BUY", tp_open=True, n_tps=3), "tg1", "chan_x", "chan_x", _rs(),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
        )
    assert "999" in result["skip_reason"]

    with db.db() as conn:
        tg_row = conn.execute(
            "SELECT status, signal_id FROM vantage_tg_signals WHERE tg_message_id='tg1'"
        ).fetchone()
        assert tg_row[0] == "pending"
        signal_id = tg_row[1]
        assert signal_id

        sig_row = conn.execute(
            "SELECT direction, stop_loss, status FROM vantage_signals WHERE signal_id=?",
            (signal_id,),
        ).fetchone()
        assert tuple(sig_row) == ("BUY", 4141.0, "pending")

        po_row = conn.execute(
            "SELECT signal_id, channel_name, direction, price, ea_ticket, status, tp_open, "
            "tps_json, pcts_json, be_at_pos, strategy FROM vantage_pending_orders WHERE ea_ticket=999"
        ).fetchone()
        assert po_row[0] == signal_id
        assert po_row[1] == "chan_x"
        assert po_row[2] == "BUY"
        assert po_row[3] == 4148.0
        assert po_row[5] == "working"
        assert po_row[6] == 1
        tps = json.loads(po_row[7])
        assert tps == {"1": 4151.0, "2": 4155.0, "3": 4160.0}
        pcts = json.loads(po_row[8])
        assert pcts == pytest.approx([0.25, 0.25, 0.25])
        assert po_row[9] == 0
        assert po_row[10] == STRATEGY_LIMIT_RUNNER
