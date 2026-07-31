"""
Unit tests for SimulationEngine.
Ported from hummingbot-api/test/vantage_mt5/test_service.py.
All tests are fully isolated — no real MT5, Claude, or Telegram credentials needed.
"""

import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Point DB at a temp file
_tmp_dir = tempfile.mkdtemp()
_TEST_DB = str(Path(_tmp_dir) / "test_forex.db")
os.environ["VANTAGE_DB_PATH"] = _TEST_DB

# Stub config before imports
_TEST_CONFIG = {
    "starting_balance":   1000.0,
    "anthropic_api_key":  "",
    "claude_model":       "claude-sonnet-4-6",
    "mt5_bridge_url":     "",
    "mt5_bridge_enabled": False,
    "db_path":            _TEST_DB,
    "sessions_dir":       str(Path(_tmp_dir) / "sessions"),
    "telegram_api_id":    "",
    "telegram_api_hash":  "",
    "telegram_phone":     "",
    "telegram_session_name": "test",
    "mt5_login":          0,
    "mt5_server":         "",
}

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from backend.src.db import database as db_module
from backend.src.runtime import SimulationEngine
from backend.src.utils.models import Tick, STRATEGY_SCALE_OUT, STRATEGY_BE_RUNNER, STRATEGY_TRAIL_STOP

db_module.init(_TEST_DB)


def _make_tick(bid=2350.00, ask=2350.30) -> Tick:
    from backend.src.utils.models import POINT_SIZE, DIGITS
    spread = round(ask - bid, 5)
    return Tick(bid=round(bid, DIGITS), ask=round(ask, DIGITS),
                mid=round((bid + ask) / 2, DIGITS),
                spread=spread, spread_points=round(spread / POINT_SIZE, 1),
                timestamp=time.time(), source="test")


def _fresh_engine() -> SimulationEngine:
    engine = SimulationEngine(_TEST_CONFIG)
    # Reset DB state
    with db_module.db() as conn:
        # Delete child tables before parents to satisfy FK constraints:
        #   partial_closes.trade_id  → simulated_trades.trade_id
        #   simulated_trades.signal_id → signals.signal_id
        conn.executescript("""
            DELETE FROM vantage_partial_closes;
            DELETE FROM vantage_simulated_trades;
            DELETE FROM vantage_signals;
            DELETE FROM vantage_claude_commentary;
            DELETE FROM vantage_telegram_log;
            UPDATE vantage_simulation_account SET balance=1000, reset_at=0 WHERE id=1;
        """)
    return engine


class TestConfig(unittest.TestCase):
    def setUp(self):
        db_module._apply_schema()

    def test_db_initialises(self):
        with db_module.db() as conn:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()}
        self.assertIn("vantage_simulated_trades", tables)
        self.assertIn("vantage_signals", tables)
        self.assertIn("telegram_messages", tables)

    def test_risk_settings_defaults(self):
        rs = db_module.get_risk_settings()
        self.assertIn("risk_per_trade_pct", rs)
        self.assertEqual(float(rs["risk_per_trade_pct"]), 0.5)

    def test_fee_settings_defaults(self):
        fs = db_module.get_fee_settings()
        self.assertIn("swap_per_lot_per_night", fs)


class TestPnlCalculation(unittest.TestCase):
    def setUp(self):
        self.engine = _fresh_engine()

    def test_buy_profit(self):
        pnl = self.engine.pnl("BUY", 2000.0, 2010.0, 1.0)
        self.assertAlmostEqual(pnl, 1000.0, places=2)  # 10 pts * 100 oz * 1 lot

    def test_buy_loss(self):
        pnl = self.engine.pnl("BUY", 2010.0, 2000.0, 1.0)
        self.assertAlmostEqual(pnl, -1000.0, places=2)

    def test_sell_profit(self):
        pnl = self.engine.pnl("SELL", 2010.0, 2000.0, 1.0)
        self.assertAlmostEqual(pnl, 1000.0, places=2)

    def test_sell_loss(self):
        pnl = self.engine.pnl("SELL", 2000.0, 2010.0, 1.0)
        self.assertAlmostEqual(pnl, -1000.0, places=2)

    def test_fractional_lots(self):
        pnl = self.engine.pnl("BUY", 2000.0, 2001.0, 0.01)
        self.assertAlmostEqual(pnl, 1.0, places=4)


class TestLotSizing(unittest.TestCase):
    def setUp(self):
        self.engine = _fresh_engine()

    def test_basic_lot_sizing(self):
        lot = self.engine.suggest_lot_size(2350.0, 2340.0, 1000.0, 0.5)
        self.assertGreaterEqual(lot, 0.01)

    def test_zero_distance_returns_min(self):
        lot = self.engine.suggest_lot_size(2350.0, 2350.0, 1000.0, 0.5)
        self.assertEqual(lot, 0.01)

    def test_lot_capped_at_max(self):
        lot = self.engine.suggest_lot_size(2350.0, 2350.01, 100000.0, 5.0)
        rs  = db_module.get_risk_settings()
        self.assertLessEqual(lot, float(rs.get("max_lot_size", 0.10)))


class TestSignalCrud(unittest.TestCase):
    def setUp(self):
        self.engine = _fresh_engine()

    def test_create_signal(self):
        result = self.engine.create_signal(
            "Test", "BUY", 2340.0, 2342.0, 2335.0,
            tp1=2350.0, tp2=2360.0,
        )
        self.assertIn("signal_id", result)
        sigs = self.engine.get_signals()
        self.assertEqual(len(sigs), 1)

    def test_cancel_signal(self):
        sig = self.engine.create_signal("Test", "SELL", 2360.0, 2362.0, 2370.0, tp1=2350.0)
        self.engine.cancel_signal(sig["signal_id"])
        sigs = self.engine.get_signals(status="cancelled")
        self.assertEqual(len(sigs), 1)

    def test_validation_rejects_bad_sl(self):
        with self.assertRaises(ValueError):
            self.engine.create_signal("Test", "BUY", 2340.0, 2342.0, 2345.0, tp1=2350.0)

    def test_validation_rejects_bad_tp(self):
        with self.assertRaises(ValueError):
            self.engine.create_signal("Test", "BUY", 2340.0, 2342.0, 2330.0, tp1=2335.0)


class TestTradeManagement(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = _fresh_engine()

    async def test_open_and_close_trade(self):
        sig = self.engine.create_signal("Test", "BUY", 2340.0, 2342.0, 2330.0,
                                         tp1=2360.0, tp2=2370.0)
        tick = _make_tick(2341.0, 2341.30)

        with patch.object(self.engine._bridge, "place_order",
                          new_callable=AsyncMock,
                          return_value={"ticket": 12345, "fill_price": 2341.30}):
            result = await self.engine.open_trade(
                signal_id=sig["signal_id"], direction="BUY",
                entry_low=2340.0, entry_high=2342.0, stop_loss=2330.0,
                tp1=2360.0, tp2=2370.0, lot_size=0.01, tick=tick,
            )
        self.assertIn("trade_id", result)

        trades = self.engine.get_open_trades()
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["direction"], "BUY")

        close_tick = _make_tick(2360.0, 2360.30)
        with patch.object(self.engine._bridge, "close_position",
                          new_callable=AsyncMock,
                          return_value={"success": True, "close_price": 2360.0}):
            close_result = await self.engine.close_trade(result["trade_id"])
        self.assertIn("net_pnl", close_result)
        self.assertGreater(close_result["net_pnl"], 0)

        trades = self.engine.get_open_trades()
        self.assertEqual(len(trades), 0)

    async def test_partial_close(self):
        sig = self.engine.create_signal("Test", "BUY", 2340.0, 2342.0, 2330.0,
                                         tp1=2360.0)
        tick = _make_tick()
        with patch.object(self.engine._bridge, "place_order",
                          new_callable=AsyncMock,
                          return_value={"ticket": 99, "fill_price": 2341.30}):
            result = await self.engine.open_trade(
                signal_id=sig["signal_id"], direction="BUY",
                entry_low=2340.0, entry_high=2342.0, stop_loss=2330.0,
                tp1=2360.0, lot_size=0.10, tick=tick,
            )
        partial = await self.engine.partial_close_trade(result["trade_id"], 0.02, 2360.0, "TP1")
        self.assertEqual(partial["lots_closed"], 0.02)
        self.assertAlmostEqual(partial["remaining_lots"], 0.08, places=4)

    async def test_sl_check(self):
        sig = self.engine.create_signal("Test", "BUY", 2340.0, 2342.0, 2330.0,
                                         tp1=2360.0)
        tick = _make_tick()
        with patch.object(self.engine._bridge, "place_order",
                          new_callable=AsyncMock,
                          return_value={"ticket": 111, "fill_price": 2341.0}):
            trade = await self.engine.open_trade(
                signal_id=sig["signal_id"], direction="BUY",
                entry_low=2340.0, entry_high=2342.0, stop_loss=2330.0,
                tp1=2360.0, lot_size=0.01, tick=tick,
            )
        open_trade = self.engine.get_open_trades()[0]
        sl_tick = _make_tick(2329.0, 2329.30)
        hit = self.engine._check_sl(open_trade, sl_tick)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[2], "SL")

    async def test_tp_check(self):
        sig = self.engine.create_signal("Test", "BUY", 2340.0, 2342.0, 2330.0,
                                         tp1=2360.0)
        tick = _make_tick()
        with patch.object(self.engine._bridge, "place_order",
                          new_callable=AsyncMock,
                          return_value={"ticket": 222, "fill_price": 2341.0}):
            trade = await self.engine.open_trade(
                signal_id=sig["signal_id"], direction="BUY",
                entry_low=2340.0, entry_high=2342.0, stop_loss=2330.0,
                tp1=2360.0, lot_size=0.01, tick=tick,
            )
        open_trade = self.engine.get_open_trades()[0]
        tp_tick = _make_tick(2365.0, 2365.30)
        hits = await self.engine._check_tp_hits(open_trade, tp_tick)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][1], 1)   # TP1


class TestPerformance(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = _fresh_engine()

    async def test_performance_empty(self):
        p = self.engine.compute_performance()
        self.assertEqual(p["closed_trades"], 0)
        self.assertEqual(p["open_trades"], 0)
        self.assertEqual(p["win_rate_pct"], 0.0)

    async def test_performance_with_trade(self):
        sig = self.engine.create_signal("Test", "BUY", 2340.0, 2342.0, 2330.0, tp1=2360.0)
        tick = _make_tick()
        with patch.object(self.engine._bridge, "place_order",
                          new_callable=AsyncMock,
                          return_value={"ticket": 333, "fill_price": 2341.0}):
            t = await self.engine.open_trade(
                signal_id=sig["signal_id"], direction="BUY",
                entry_low=2340.0, entry_high=2342.0, stop_loss=2330.0,
                tp1=2360.0, lot_size=0.01, tick=tick,
            )
        with patch.object(self.engine._bridge, "close_position",
                          new_callable=AsyncMock,
                          return_value={"success": True, "close_price": 2360.0}):
            await self.engine.close_trade(t["trade_id"])
        p = self.engine.compute_performance()
        self.assertEqual(p["closed_trades"], 1)
        self.assertEqual(p["open_trades"], 0)
        self.assertEqual(p["win_rate_pct"], 100.0)
        self.assertGreater(p["total_net_pnl"], 0)


class TestFeeCalculation(unittest.TestCase):
    def setUp(self):
        self.engine = _fresh_engine()

    def test_fee_structure(self):
        fees = self.engine.calculate_fees(0.01, 0.30)
        self.assertIn("spread_cost", fees)
        self.assertIn("slippage_cost", fees)
        self.assertIn("total_cost", fees)
        self.assertGreater(fees["total_cost"], 0)

    def test_swap_only_overnight(self):
        fees_no_swap  = self.engine.calculate_fees(0.01, 0.30, hold_hours=23)
        fees_with_swap = self.engine.calculate_fees(0.01, 0.30, hold_hours=25)
        self.assertEqual(fees_no_swap["swap_cost"], 0.0)
        # Swap is negative (cost) so absolute value should be > 0
        self.assertNotEqual(fees_with_swap["swap_cost"], 0.0)


if __name__ == "__main__":
    unittest.main()
