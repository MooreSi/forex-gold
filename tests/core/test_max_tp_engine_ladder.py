"""Max TP Hit for a trade one of the app's own engines opened is measured
against the TPs the trade was actually given, not the engine's signal ladder.

The Reversal Engine writes a $1-spaced ladder measured from its entry ZONE
(e.g. zone top 4184.52, TP1..TP4 = 4186.02..4189.02). A template then places
the real trade at the fill with its own, wider ladder (TP1 4188.55). Measured
against the signal ladder, a trade that never reached its own TP1 and was
stopped out read "TP3" on the Trades page (demo, 2026-10-12, ticket
2170604954). Telegram channel trades keep the signal ladder: that is the
owner's v0.23 choice and test_max_tp_hit_characterization pins it.

Read-only candle fetches plus DB writes to max_tp_hit; no order is touched.
"""
import asyncio
import time
from unittest import mock

from backend.src.db import database as db
from backend.src.services.positions import max_tp


# Ticket 2170604954 as recorded on the demo account.
_SIG_LADDER = (4186.02, 4187.02, 4188.02, 4189.02, 4191.02, 4193.02, None, 4213.02)
_TRADE_LADDER = (4188.55, 4189.55, 4191.55, 4194.55, None, None, None, None)


def _insert(trade_id, tg_source, trade_tps=_TRADE_LADDER, sig_tps=_SIG_LADDER,
            max_tp_hit=None, status="closed"):
    now = time.time()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at, tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", "BUY", 4181.52, 4184.52, 4179.02, "active", now,
             *sig_tps),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "status, open_time, close_time, tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8, "
            "max_tp_hit, net_pnl, strategy, tg_source, mt5_ticket) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", "BUY", 4181.52, 4184.52, 4184.48, 0.04, 0.0,
             4179.55, status, now - 7000, now - 2000, *trade_tps, max_tp_hit, -19.72,
             "template:30 TP1 SL50 and Trail", tg_source, 2170604954),
        )


def _stored(trade_id):
    with db.db() as conn:
        return conn.execute(
            "SELECT max_tp_hit FROM vantage_simulated_trades WHERE trade_id=?",
            (trade_id,)).fetchone()[0]


def _sweep(high):
    bridge = mock.Mock(get_candles_range=mock.AsyncMock(
        return_value=[{"high": high, "low": 4179.0}]))
    with mock.patch("backend.src.services.cluster.sync.ledger.push_trade_closed"):
        asyncio.run(max_tp.max_tp_checker_sweep(bridge))


def test_engine_trade_below_its_own_tp1_reads_none(fresh_db):
    # 4188.40 is past signal TP3 (4188.02) but short of the trade's TP1 (4188.55).
    _insert("re-short", "Reversal Engine")
    _sweep(4188.40)
    assert _stored("re-short") == "none"


def test_engine_trade_is_graded_on_its_own_ladder(fresh_db):
    # Past trade TP2 (4189.55), short of TP3 (4191.55). The signal ladder
    # would have called this TP5.
    _insert("re-tp2", "Reversal Engine")
    _sweep(4191.10)
    assert _stored("re-tp2") == "TP2"


def test_every_internal_engine_uses_its_own_ladder(fresh_db):
    _insert("tpa", "Trend PA Engine")
    _sweep(4188.40)
    assert _stored("tpa") == "none"


def test_engine_trade_with_no_own_tps_falls_back_to_signal_ladder(fresh_db):
    _insert("re-bare", "Reversal Engine", trade_tps=(None,) * 8)
    _sweep(4188.40)
    assert _stored("re-bare") == "TP3"


def test_telegram_channel_trade_still_uses_the_signal_ladder(fresh_db):
    _insert("tg", "Gold Diggers VIP")
    _sweep(4188.40)
    assert _stored("tg") == "TP3"


def test_engine_ladder_repair_clears_engine_rows_once(fresh_db):
    _insert("re-old", "Reversal Engine", max_tp_hit="TP3")
    _insert("tg-old", "Gold Diggers VIP", max_tp_hit="TP3")
    _insert("re-open", "Reversal Engine", max_tp_hit="TP1", status="open")

    assert max_tp.repair_engine_max_tp_once() == 1
    assert _stored("re-old") is None
    assert _stored("tg-old") == "TP3"
    assert _stored("re-open") == "TP1"

    # A value recomputed after the repair is never cleared again.
    with db.db() as conn:
        conn.execute("UPDATE vantage_simulated_trades SET max_tp_hit='none' "
                     "WHERE trade_id='re-old'")
    assert max_tp.repair_engine_max_tp_once() == 0
    assert _stored("re-old") == "none"


def test_sweep_runs_the_repair_and_recomputes(fresh_db):
    _insert("re-wrong", "Reversal Engine", max_tp_hit="TP3")
    _sweep(4188.40)
    assert _stored("re-wrong") == "none"
