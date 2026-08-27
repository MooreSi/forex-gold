"""core_db_max_tp.get_rr_map_by_ticket() -- the Closed Trades page's R:R
column. Covers the 2026-07-24 fix: realized R (actual net_pnl relative to
the dollar risk taken at entry) instead of a static TP1-vs-SL plan ratio
that showed the same number for every trade regardless of strategy or how
far the trade actually ran."""

import pytest

from backend.src.db import database as db
from backend.src.services.positions import max_tp_repo as maxtp


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _insert_trade(trade_id, direction="BUY", entry_price=2400.0, stop_loss=2390.0,
                   lot_size=0.10, net_pnl=10.0, mt5_ticket=555, status="closed"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", direction, entry_price, entry_price, stop_loss, "active", 0.0),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "status, open_time, net_pnl, strategy, mt5_ticket) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", direction, entry_price, entry_price, entry_price,
             lot_size, 0.0, stop_loss, status, 0.0, net_pnl, "scale_out", mt5_ticket),
        )


def test_buy_runner_shows_realized_r_above_one(fresh_db):
    # Risked (2400-2390)*0.10*100 = $100; banked $350 -- ran well past a
    # single-TP plan ratio would ever show.
    _insert_trade("t1", direction="BUY", entry_price=2400.0, stop_loss=2390.0,
                  lot_size=0.10, net_pnl=350.0, mt5_ticket=111)
    rr_map = maxtp.get_rr_map_by_ticket()
    assert rr_map["111"] == pytest.approx(3.5)


def test_sell_trade_realized_r(fresh_db):
    _insert_trade("t2", direction="SELL", entry_price=2400.0, stop_loss=2410.0,
                  lot_size=0.10, net_pnl=200.0, mt5_ticket=222)
    rr_map = maxtp.get_rr_map_by_ticket()
    assert rr_map["222"] == pytest.approx(2.0)


def test_losing_trade_shows_negative_r(fresh_db):
    _insert_trade("t3", direction="BUY", entry_price=2400.0, stop_loss=2390.0,
                  lot_size=0.10, net_pnl=-95.0, mt5_ticket=333)
    rr_map = maxtp.get_rr_map_by_ticket()
    assert rr_map["333"] == pytest.approx(-0.95)


def test_full_sl_hit_is_close_to_minus_one_r(fresh_db):
    # A clean full-lot stop-out nets close to -1R (spread/commission drag
    # aside) -- not the old TP1-only metric, which had no notion of the
    # trade's actual outcome at all.
    _insert_trade("t4", direction="BUY", entry_price=2400.0, stop_loss=2390.0,
                  lot_size=0.10, net_pnl=-98.0, mt5_ticket=444)
    rr_map = maxtp.get_rr_map_by_ticket()
    assert rr_map["444"] == pytest.approx(-0.98)


def test_zero_risk_excluded(fresh_db):
    _insert_trade("t5", direction="BUY", entry_price=2400.0, stop_loss=2400.0,
                  lot_size=0.10, net_pnl=10.0, mt5_ticket=555)
    rr_map = maxtp.get_rr_map_by_ticket()
    assert "555" not in rr_map


def test_breakeven_trade_maps_to_zero_not_excluded(fresh_db):
    # net_pnl is NOT NULL DEFAULT 0 in the schema -- a trade that nets
    # exactly $0 is a real, meaningful breakeven outcome (0R), not a
    # missing-data case to exclude.
    _insert_trade("t6", direction="BUY", entry_price=2400.0, stop_loss=2390.0,
                  lot_size=0.10, net_pnl=0.0, mt5_ticket=666)
    rr_map = maxtp.get_rr_map_by_ticket()
    assert rr_map["666"] == pytest.approx(0.0)


def test_missing_mt5_ticket_excluded(fresh_db):
    # mt5_ticket IS the only genuinely-nullable column this query filters
    # on (entry_price/stop_loss/lot_size/net_pnl are all NOT NULL in the
    # schema) -- a trade never sent to a live ticket has nothing to key
    # the returned map by.
    _insert_trade("t9", direction="BUY", entry_price=2400.0, stop_loss=2390.0,
                  lot_size=0.10, net_pnl=10.0, mt5_ticket=None)
    rr_map = maxtp.get_rr_map_by_ticket()
    assert rr_map == {}


def test_prefers_original_signal_stop_over_breakeven_moved_trade_stop(fresh_db):
    # Reproduces the 2026-07-24 finding: after a breakeven move, the
    # trade's own stop_loss lands on entry_price (zero risk if used
    # directly), but the linked signal's stop_loss -- never touched by
    # the breakeven-move code paths -- still holds what was actually
    # risked at entry.
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("sig-tbe", "SELL", 4053.7, 4053.7, 4061.5, "active", 0.0),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "status, open_time, net_pnl, strategy, mt5_ticket) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("tbe", "sig-tbe", "SELL", 4053.7, 4053.7, 4053.7, 0.10, 0.09, 4053.7,
             "closed", 0.0, 107.6, "conservative_trial", 999888),
        )
    rr_map = maxtp.get_rr_map_by_ticket()
    # Risked (4061.5-4053.7)*0.10*100 = $78; banked $107.60.
    assert rr_map["999888"] == pytest.approx(107.6 / 78.0)


def test_multiple_trades_map_correctly(fresh_db):
    _insert_trade("t7", direction="BUY", entry_price=2400.0, stop_loss=2390.0,
                  lot_size=0.10, net_pnl=150.0, mt5_ticket=777)
    _insert_trade("t8", direction="SELL", entry_price=2400.0, stop_loss=2410.0,
                  lot_size=0.20, net_pnl=-40.0, mt5_ticket=888)
    rr_map = maxtp.get_rr_map_by_ticket()
    assert rr_map["777"] == pytest.approx(1.5)
    assert rr_map["888"] == pytest.approx(-0.2)


# ── initial_risk (2026-08-07) ────────────────────────────────────────────
# The entry/stop/lot reconstruction above is only ever as good as the two
# stop columns it picks between, and for an EA Template grid both are
# wrong: the signal's stop is not what got placed (the template's sl_pips
# replaces it), and lot_size is one leg of a basket whose net_pnl covers
# every leg. initial_risk records the real figure at open instead.


def _insert_trade_with_initial_risk(trade_id, *, direction, entry_price, sig_stop,
                                    lot_size, net_pnl, mt5_ticket, initial_risk,
                                    strategy="template:Grid - Zone Mode"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", direction, entry_price, entry_price, sig_stop, "active", 0.0),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "status, open_time, net_pnl, strategy, mt5_ticket, initial_sl, initial_risk) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", direction, entry_price, entry_price, entry_price,
             lot_size, 0.0, entry_price, "closed", 0.0, net_pnl, strategy, mt5_ticket,
             sig_stop, initial_risk),
        )


def test_initial_risk_wins_over_the_signal_stop(fresh_db):
    # Live shape, ticket 1726323015: a "Grid - Zone Mode" SELL whose single
    # leg stopped out at the template's own 40-pip stop ($39.60 on 0.1 lot
    # gold) -- a clean -1R. The signal's stop was only 2.34 away, so
    # reconstructing risk from it reported -1.69R for a full stop-out.
    _insert_trade_with_initial_risk(
        "tir1", direction="SELL", entry_price=4284.16, sig_stop=4286.50,
        lot_size=0.10, net_pnl=-39.60, mt5_ticket=1726323015, initial_risk=40.0)
    rr_map = maxtp.get_rr_map_by_ticket()
    assert rr_map["1726323015"] == pytest.approx(-0.99)


def test_initial_risk_covers_every_grid_leg_not_just_the_promoting_one(fresh_db):
    # Ticket 1726213592: a 2-leg grid. core_profit_sync sums BOTH legs into
    # net_pnl (-$76.00) while lot_size stays the promoting leg's 0.10, so
    # any risk derived from lot_size alone is half the real basket and the
    # magnitude comes out roughly doubled -- this reported -2.20R.
    _insert_trade_with_initial_risk(
        "tir2", direction="SELL", entry_price=4275.30, sig_stop=4278.75,
        lot_size=0.10, net_pnl=-76.00, mt5_ticket=1726213592, initial_risk=80.0)
    rr_map = maxtp.get_rr_map_by_ticket()
    assert rr_map["1726213592"] == pytest.approx(-0.95)


def test_initial_risk_survives_a_breakeven_move_on_the_trade_stop(fresh_db):
    # The reason the signal stop was preferred in the first place: a winner
    # that moved to breakeven has trade.stop_loss sitting on entry_price.
    # initial_risk is written once at open and never touched, so it needs
    # no such workaround -- _insert_trade_with_initial_risk deliberately
    # stores stop_loss == entry_price to prove it.
    _insert_trade_with_initial_risk(
        "tir3", direction="BUY", entry_price=4276.32, sig_stop=4268.50,
        lot_size=0.10, net_pnl=149.90, mt5_ticket=1715704187, initial_risk=80.0)
    rr_map = maxtp.get_rr_map_by_ticket()
    assert rr_map["1715704187"] == pytest.approx(149.90 / 80.0)


def test_falls_back_to_the_stop_columns_when_initial_risk_is_absent(fresh_db):
    # Every row opened before the column existed. Unchanged behaviour --
    # approximate, but the best those rows can support.
    _insert_trade("tir4", direction="BUY", entry_price=2400.0, stop_loss=2390.0,
                  lot_size=0.10, net_pnl=150.0, mt5_ticket=4444)
    rr_map = maxtp.get_rr_map_by_ticket()
    assert rr_map["4444"] == pytest.approx(1.5)


def test_zero_initial_risk_falls_back_rather_than_dividing_by_zero(fresh_db):
    _insert_trade_with_initial_risk(
        "tir5", direction="BUY", entry_price=2400.0, sig_stop=2390.0,
        lot_size=0.10, net_pnl=150.0, mt5_ticket=5555, initial_risk=0.0)
    rr_map = maxtp.get_rr_map_by_ticket()
    # initial_risk of 0 is not usable; the signal stop still is ($100 risk).
    assert rr_map["5555"] == pytest.approx(1.5)
