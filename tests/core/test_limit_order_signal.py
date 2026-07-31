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

from backend.src.db import database as db
from backend.src.services.trading import limit_order_signal as los
from backend.src.services.risk import strategy_params as sp
from backend.src.utils.models import STRATEGY_LIMIT_RUNNER, Tick


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
    def __init__(self, healthy=True, ack=None, raise_exc=None,
                 open_trade_ack=None, open_trade_raise=None):
        self._healthy = healthy
        self._ack = ack if ack is not None else {"type": "pending_order_placed", "ticket": 555}
        self._raise = raise_exc
        self._open_trade_ack = open_trade_ack if open_trade_ack is not None else {
            "type": "trade_opened", "ticket": 777, "fill_price": 0.0,
        }
        self._open_trade_raise = open_trade_raise
        self.calls = []
        self.open_trade_calls = []

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

    async def open_trade(self, trade_id, direction, lot_size, stop_loss, tps, strategy,
                         pcts=None, be_at_pos=None, trail_mode=None, template=None,
                         timeout=5.0):
        self.open_trade_calls.append(dict(
            trade_id=trade_id, direction=direction, lot_size=lot_size,
            stop_loss=stop_loss, tps=dict(tps), strategy=strategy,
            pcts=list(pcts) if pcts is not None else None, be_at_pos=be_at_pos,
        ))
        if self._open_trade_raise:
            raise self._open_trade_raise
        return self._open_trade_ack


class _FakeBridge:
    def __init__(self, bid, ask):
        self._tick = Tick(
            bid=bid, ask=ask, mid=(bid + ask) / 2, spread=ask - bid,
            spread_points=(ask - bid) * 100, timestamp=0.0, source="fake",
        )

    async def get_tick(self):
        return self._tick


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


def _rs(realign=False):
    return {"risk_per_trade_pct": 0.5, "strategy_lot_size": 0,
            "lk_entry_realignment": 1 if realign else 0}


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
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=None):
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
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
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
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
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
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
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
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
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
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
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
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
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
    with patch("backend.src.controllers.sync.client.get_instance", return_value=None), \
         patch("backend.src.controllers.sync.server.get_instance", return_value=None), \
         patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
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
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
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
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
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


# ── Entry Realignment ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_realignment_off_by_default_ignores_price_breach(fresh_db):
    """rs() defaults lk_entry_realignment to 0 -- even with a bridge whose
    tick shows the zone already breached, behaviour must be unchanged: a
    resting pending order attempt, not a market fallback."""
    await _insert_tg_row("tg1")
    fake_ea = _FakeEA()
    bridge = _FakeBridge(bid=4149.8, ask=4150.0)  # ask 4150 > entry_high 4148
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
        await los.handle_limit_order_signal(
            _parsed("BUY"), "tg1", "chan", "chan", _rs(realign=False),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
            bridge=bridge,
        )
    assert len(fake_ea.calls) == 1
    assert fake_ea.open_trade_calls == []


@pytest.mark.asyncio
async def test_realignment_enabled_no_bridge_falls_back_to_pending(fresh_db):
    """Toggle on but no bridge wired through (defensive default) -- must
    not error, just behave as if realignment were off."""
    await _insert_tg_row("tg1")
    fake_ea = _FakeEA()
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
        await los.handle_limit_order_signal(
            _parsed("BUY"), "tg1", "chan", "chan", _rs(realign=True),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
        )
    assert len(fake_ea.calls) == 1
    assert fake_ea.open_trade_calls == []


@pytest.mark.asyncio
async def test_realignment_enabled_zone_not_breached_still_places_pending(fresh_db):
    await _insert_tg_row("tg1")
    fake_ea = _FakeEA()
    bridge = _FakeBridge(bid=4146.8, ask=4147.0)  # ask 4147 < entry_high 4148 -- not breached
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
        await los.handle_limit_order_signal(
            _parsed("BUY"), "tg1", "chan", "chan", _rs(realign=True),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
            bridge=bridge,
        )
    assert len(fake_ea.calls) == 1
    assert fake_ea.open_trade_calls == []


@pytest.mark.asyncio
async def test_realignment_buy_breach_opens_at_market_with_shifted_levels(fresh_db):
    await _insert_tg_row("tg1")
    fake_ea = _FakeEA(open_trade_ack={"type": "trade_opened", "ticket": 777, "fill_price": 4150.0})
    bridge = _FakeBridge(bid=4149.8, ask=4150.0)  # breaches entry_high 4148 by 2.0
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
        result = await los.handle_limit_order_signal(
            _parsed("BUY", tp_open=False, n_tps=3), "tg1", "chan", "chan", _rs(realign=True),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
            bridge=bridge,
        )
    assert fake_ea.calls == []  # never attempted the resting order
    assert len(fake_ea.open_trade_calls) == 1
    call = fake_ea.open_trade_calls[0]
    assert call["direction"] == "BUY"
    assert call["strategy"] == STRATEGY_LIMIT_RUNNER
    # delta = 4150.0 (ask) - 4148.0 (entry_high) = +2.0, shifted onto SL/TPs
    assert call["stop_loss"] == pytest.approx(4143.0)  # 4141.0 + 2.0
    assert call["tps"] == pytest.approx({1: 4153.0, 2: 4157.0, 3: 4162.0})
    assert "realigned" in result["skip_reason"].lower()


@pytest.mark.asyncio
async def test_realignment_sell_breach_opens_at_market_with_shifted_levels(fresh_db):
    await _insert_tg_row("tg1")
    fake_ea = _FakeEA(open_trade_ack={"type": "trade_opened", "ticket": 778, "fill_price": 4140.0})
    bridge = _FakeBridge(bid=4140.0, ask=4140.2)  # breaches entry_low 4142 by -2.0
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
        result = await los.handle_limit_order_signal(
            _parsed("SELL", tp_open=False, n_tps=3), "tg1", "chan", "chan", _rs(realign=True),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
            bridge=bridge,
        )
    assert fake_ea.calls == []
    call = fake_ea.open_trade_calls[0]
    assert call["direction"] == "SELL"
    # delta = 4140.0 (bid) - 4142.0 (entry_low) = -2.0
    assert call["stop_loss"] == pytest.approx(4187.0)  # 4189.0 - 2.0
    assert call["tps"] == pytest.approx({1: 4174.0, 2: 4170.0, 3: 4166.0})
    assert "realigned" in result["skip_reason"].lower()


@pytest.mark.asyncio
async def test_realignment_ea_rejection_reported_as_skip_reason(fresh_db):
    await _insert_tg_row("tg1")
    fake_ea = _FakeEA(open_trade_ack={"type": "trade_open_failed", "error": "No money"})
    bridge = _FakeBridge(bid=4149.8, ask=4150.0)
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
        result = await los.handle_limit_order_signal(
            _parsed("BUY"), "tg1", "chan", "chan", _rs(realign=True),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
            bridge=bridge,
        )
    assert "No money" in result["skip_reason"]

    with db.db() as conn:
        row = conn.execute(
            "SELECT status FROM vantage_tg_signals WHERE tg_message_id='tg1'"
        ).fetchone()
        assert row[0] == "new"  # unchanged -- no signal_id was ever assigned


@pytest.mark.asyncio
async def test_realignment_writes_market_order_row_no_pending_order_row(fresh_db):
    await _insert_tg_row("tg1")
    fake_ea = _FakeEA(open_trade_ack={"type": "trade_opened", "ticket": 900, "fill_price": 4150.0})
    bridge = _FakeBridge(bid=4149.8, ask=4150.0)
    with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=fake_ea):
        await los.handle_limit_order_signal(
            _parsed("BUY", tp_open=True, n_tps=3), "tg1", "chan_x", "chan_x", _rs(realign=True),
            sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
            skip_reason="",
            get_trading_balance_fn=_balance, suggest_lot_size_fn=_lot_size,
            bridge=bridge,
        )
    with db.db() as conn:
        tg_row = conn.execute(
            "SELECT status, signal_id FROM vantage_tg_signals WHERE tg_message_id='tg1'"
        ).fetchone()
        assert tg_row[0] == "active"
        signal_id = tg_row[1]

        trade_row = conn.execute(
            "SELECT mt5_ticket, entry_price, stop_loss, status, strategy, tg_source, "
            "managed_by, order_type, pending_placed_at FROM vantage_simulated_trades "
            "WHERE signal_id=?", (signal_id,),
        ).fetchone()
        assert trade_row[0] == 900
        assert trade_row[1] == pytest.approx(4150.0)
        assert trade_row[2] == pytest.approx(4143.0)
        assert trade_row[3] == "open"
        assert trade_row[4] == STRATEGY_LIMIT_RUNNER
        assert trade_row[5] == "chan_x"
        assert trade_row[6] == "ea"
        assert trade_row[7] == "market"
        assert trade_row[8] is None

        po_count = conn.execute(
            "SELECT COUNT(*) FROM vantage_pending_orders WHERE signal_id=?", (signal_id,),
        ).fetchone()[0]
        assert po_count == 0
