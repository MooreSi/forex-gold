"""The final close message carries Total pips and Risk:Reward; the partial
close message deliberately does not (neither figure is settled until the
position is fully out).

The pip figure is derived from realised MONEY rather than (exit - entry).
That distinction is the point of these tests: a laddered trade banks most
of its move in partials at prices the final exit never revisits, so the raw
price move understates -- or, as in test_partials_price_move_zero_but_pips_
positive, completely inverts -- what the trade actually achieved.
"""
import os
import tempfile

import pytest

from forex_trader.core import database as db
from forex_trader.core import telegram_alerts as ta


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    _reset_thread_local_connection()
    os.remove(path)


def _trade(**over):
    t = {
        "trade_id": "t1", "direction": "BUY", "mt5_ticket": 555,
        "entry_price": 4400.00, "lot_size": 0.10,
        "initial_sl": 4390.00,          # 100 pips of risk
        "initial_risk": 100.0,          # 10.00 price x 100oz x 0.1 lot
        "close_price": 4410.00, "net_pnl": 100.0, "mt5_profit": 100.0,
        "strategy": "template:Asian Reversal - ATR", "tg_source": "Reversal Engine",
        "close_time": 1785354753, "activated_at": 1785353007,
    }
    t.update(over)
    return t


def _lines(trade, **kw):
    msg = ta.fmt_trade_close(trade, {"close_price": trade["close_price"], "reason": "TP"}, {}, **kw)
    return msg, [l for l in msg.split("\n") if l.startswith(("Total pips", "Risk:Reward"))]


def test_winner_shows_pips_and_r(fresh_db):
    msg, got = _lines(_trade())
    assert "Total pips: +100.0" in msg
    assert "Risk:Reward: 1 : 1.00  (1.00R)" in msg
    assert len(got) == 2


def test_full_stop_reads_minus_one_r(fresh_db):
    msg, _ = _lines(_trade(close_price=4390.0, net_pnl=-100.0, mt5_profit=-100.0))
    assert "Total pips: -100.0" in msg
    # A ratio is meaningless on a loss -- signed R only, no "1 : x".
    assert "Risk:Reward: -1.00R" in msg
    assert "1 : " not in msg.split("Risk:Reward:")[1].split("\n")[0]


def test_loss_cut_early_reads_better_than_minus_one_r(fresh_db):
    msg, _ = _lines(_trade(close_price=4396.0, net_pnl=-40.0, mt5_profit=-40.0))
    assert "Risk:Reward: -0.40R" in msg


def test_partials_price_move_zero_but_pips_positive(fresh_db):
    """The case that forced money-derived pips. Live SELL closed its last
    portion at exactly its entry -- 0 pips by price -- having already
    realised +$43.29 through partial closes."""
    t = _trade(direction="SELL", entry_price=4362.18, close_price=4362.18,
               initial_sl=4374.15, initial_risk=119.7,
               net_pnl=43.29, mt5_profit=43.29)
    msg, _ = _lines(t)
    assert "Total pips: +43.3" in msg, "raw price move is 0.0 -- must not report that"
    assert "Risk:Reward: 1 : 0.36" in msg


def test_pips_and_r_stay_mutually_consistent(fresh_db):
    """R x risk_pips == total_pips, so the two lines can never disagree."""
    for cp, pnl in ((4415.0, 150.0), (4385.0, -150.0), (4400.0, 0.0), (4433.0, 330.0)):
        t = _trade(close_price=cp, net_pnl=pnl, mt5_profit=pnl)
        pips, r = ta._pips_and_rr(t, pnl)
        risk_pips = abs(t["entry_price"] - t["initial_sl"]) * 10
        assert abs(r * risk_pips - pips) < 1e-6


def test_multi_leg_grid_uses_initial_risk_not_row_lot(fresh_db):
    """An EA Template grid's row lot_size is only the promoting leg's, while
    profit sums every leg -- so profit/(lot_size*100) would overstate the
    move by roughly the leg count. initial_risk is per-filled-leg, so the
    R stays honest."""
    t = _trade(lot_size=0.10, initial_risk=300.0, net_pnl=150.0, mt5_profit=150.0)
    pips, r = ta._pips_and_rr(t, 150.0)
    assert r == pytest.approx(0.5)                    # 150 / 300, not 150/(0.1*100)
    assert pips == pytest.approx(0.5 * 100)           # 0.5R x 100 risk pips


def test_falls_back_when_initial_risk_missing(fresh_db):
    """Rows predating initial_sl/initial_risk still get pips from money over
    the row's own size; R only when a stop distance exists."""
    t = _trade(initial_risk=None)
    pips, r = ta._pips_and_rr(t, 100.0)
    assert pips == pytest.approx(100.0)               # 100 / (0.1*100) = 10.00 price = 100 pips
    assert r == pytest.approx(1.0)                    # risk_pips still derivable from initial_sl

    t2 = _trade(initial_risk=None, initial_sl=None)
    pips2, r2 = ta._pips_and_rr(t2, 100.0)
    assert pips2 == pytest.approx(100.0)
    assert r2 is None                                 # no stop recorded -> no honest R


def test_unknown_entry_suppresses_both_lines(fresh_db):
    """No entry price means profit itself is untrustworthy (the $-16086
    placeholder bug) -- neither figure may be printed."""
    t = _trade(entry_price=0.0, mt5_profit=None, net_pnl=-16086.0,
               initial_sl=None, initial_risk=None)
    msg, got = _lines(t)
    assert got == []
    assert "Profit: unknown" in msg


def test_partial_close_message_has_neither(fresh_db):
    msg = ta.fmt_mt5_partial_close(_trade(), 0.05, 4410.0, 0.05, 50.0, "TP")
    assert "Total pips" not in msg
    assert "Risk:Reward" not in msg
