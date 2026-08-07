"""history._comment_attribution_maps -- recovering a broker position's
channel from the order comment it carries, for the positions that have no
vantage_simulated_trades row of their own.

Both the Closed Trades table and the calendar's day-detail view depend on
this: before it existed at module level the calendar had no comment fallback
at all, so every EA Template sibling leg showed "Unknown" there while the
table beside it named the channel correctly (found live 2026-08-06).
"""
import os
import tempfile
import time

import pytest

from forex_trader.core import database as db
from forex_trader.ui.pages import history


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


def _insert_trade(trade_id, tg_source, strategy, signal_id=None, max_tp_hit=None):
    signal_id = signal_id or f"sig-for-{trade_id}"
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (signal_id, "BUY", 3999.0, 4001.0, 3990.0, "activated", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades "
            "(trade_id, signal_id, direction, entry_low, entry_high, entry_price, "
            " lot_size, remaining_lots, stop_loss, status, open_time, strategy, tg_source, "
            " max_tp_hit) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, signal_id, "BUY", 3999.0, 4001.0, 4000.0,
             0.01, 0.01, 3990.0, "closed", time.time(), strategy, tg_source,
             max_tp_hit),
        )


def test_entry_deal_comments_takes_the_opening_deal_only():
    """Only the entry==0 deal carries the comment the order was placed with;
    the closing deal's comment is the broker's own ("[sl 4046.50]")."""
    by_pos = {
        111: [
            {"entry": 1, "comment": "[tp 4100.00]"},
            {"entry": 0, "comment": "ea:5b88a61e-6g3"},
        ],
        222: [{"entry": 1, "comment": "[sl 3990.00]"}],   # no opening deal
    }
    assert history._entry_deal_comments(by_pos) == {"111": "ea:5b88a61e-6g3"}


def test_template_leg_inherits_channel_from_its_trade(fresh_db):
    """A grid template opens one broker position per leg but Python keeps a
    SINGLE row, so every leg except the one that promoted that row can only
    be linked back through the EA's comment prefix."""
    _insert_trade("5b88a61e-6c22-4d", "GOLD DIGGERS INSTITUTIONAL",
                  "template:GD Institutional - Grid")
    src, strat, _ = history._comment_attribution_maps({
        "900001": "ea:5b88a61e-6a1",
        "900002": "ea:5b88a61e-6g2",
    })
    assert src == {"900001": "GOLD DIGGERS INSTITUTIONAL",
                   "900002": "GOLD DIGGERS INSTITUTIONAL"}
    assert strat["900002"] == "Template: GD Institutional - Grid"


def test_orphaned_sig_comment_recovers_channel_via_signal_id(fresh_db):
    """"sig:<signal_id[:8]>" is this app's own non-template order comment, so
    a position carrying it IS ours -- reaching the comment fallback at all
    means the trade row lost its mt5_ticket link."""
    _insert_trade("aaaabbbb-1111-42", "Gold Diggers VIP", "scale_out",
                  signal_id="4ea5f031ab12cd34")
    src, strat, _ = history._comment_attribution_maps({"900003": "sig:4ea5f031"})
    assert src == {"900003": "Gold Diggers VIP"}
    assert strat == {"900003": "Scale Out"}


def test_copier_positions_are_named_not_left_unknown(fresh_db):
    """The third-party copier EA's positions are not this app's trades and
    never get a row, so no channel can honestly be attributed -- but naming
    the copier beats the bare "Unknown"/"—" they showed before, which was
    indistinguishable from a genuine attribution failure."""
    src, strat, _ = history._comment_attribution_maps({
        "900004": "C2_LDBD_25533_ANC",
        "900005": "C1_SGBD_17794_ANC",
        "900006": "C2_LDBD_25533_PEN",
    })
    assert src == {"900004": "Copier EA (C2)",
                   "900005": "Copier EA (C1)",
                   "900006": "Copier EA (C2)"}
    assert set(strat.values()) == {"External"}


def test_unrecognised_comments_are_left_for_the_caller_to_default(fresh_db):
    """Broker-generated and genuinely unknown comments must NOT be given a
    made-up attribution -- the caller's own "Unknown" placeholder is the
    honest answer for them."""
    src, strat, _ = history._comment_attribution_maps({
        "900007": "positionOrder",
        "900008": "[sl 4046.50]",
        "900009": "",
    })
    assert src == {}
    assert strat == {}


def test_template_prefix_with_no_matching_trade_is_not_invented(fresh_db):
    """An "ea:" comment whose trade row no longer exists resolves to nothing
    rather than to some other trade's channel."""
    _insert_trade("ffffffff-0000-49", "Reversal Engine", "conservative")
    src, _, _ = history._comment_attribution_maps({"900010": "ea:5b88a61e-6g1"})
    assert src == {}


# ── Max TP Hit attribution ───────────────────────────────────────────────────
# Max TP is only ever computed onto a vantage_simulated_trades row, so before
# these the whole comment-attributed part of the Closed Trades table -- every
# template sibling leg and every copier position -- showed a permanent "..."
# tooltipped "Updating in 30 min" that no sweep could ever replace. Measured on
# the demo account: 1913 of 2498 rendered broker positions were stuck on it.

def test_template_leg_inherits_max_tp_from_its_trade(fresh_db):
    """Every leg of a template trade belongs to one signal and is measured
    against that signal's TP ladder, so the parent row's value is the answer
    for the legs too."""
    _insert_trade("5b88a61e-6c22-4d", "GOLD DIGGERS INSTITUTIONAL",
                  "template:GD Institutional - Grid", max_tp_hit="TP4")

    _, _, max_tp = history._comment_attribution_maps({
        "900001": "ea:5b88a61e-6a1",
        "900002": "ea:5b88a61e-6g2",
    })

    assert max_tp == {"900001": "TP4", "900002": "TP4"}


def test_leg_of_an_uncomputed_trade_stays_pending_not_blank(fresh_db):
    """A parent whose 30-min window hasn't elapsed yet must leave its legs
    unset, so they keep showing "..." and pick the real value up later --
    rather than being frozen at a wrong answer."""
    _insert_trade("5b88a61e-6c22-4d", "GOLD DIGGERS INSTITUTIONAL",
                  "template:GD Institutional - Grid", max_tp_hit=None)

    _, _, max_tp = history._comment_attribution_maps({"900001": "ea:5b88a61e-6a1"})

    assert max_tp == {}


def test_computed_sibling_row_wins_over_an_uncomputed_one(fresh_db):
    """A template trade can leave more than one row sharing a trade_id
    prefix; picking an arbitrary one would blank the column for legs whose
    sibling was already computed."""
    # Both share the "5b88a61e-6" prefix the "ea:" comment carries.
    _insert_trade("5b88a61e-6000-4a", "GOLD DIGGERS INSTITUTIONAL",
                  "template:GD Institutional - Grid", max_tp_hit=None)
    _insert_trade("5b88a61e-6111-4b", "GOLD DIGGERS INSTITUTIONAL",
                  "template:GD Institutional - Grid", max_tp_hit="TP2")

    _, _, max_tp = history._comment_attribution_maps({"900001": "ea:5b88a61e-6a1"})

    assert max_tp == {"900001": "TP2"}


def test_orphaned_sig_comment_inherits_max_tp_via_signal_id(fresh_db):
    _insert_trade("aaaabbbb-1111-42", "Gold Diggers VIP", "scale_out",
                  signal_id="4ea5f031ab12cd34", max_tp_hit="TP1")

    _, _, max_tp = history._comment_attribution_maps({"900003": "sig:4ea5f031"})

    assert max_tp == {"900003": "TP1"}


def test_copier_positions_are_marked_not_applicable(fresh_db):
    """The copier EA's positions are not this app's trades and have no TP
    ladder of ours to measure against, so promising an update in 30 minutes
    was a lie -- "n/a" renders as a plain dash instead."""
    _, _, max_tp = history._comment_attribution_maps({
        "900004": "C2_LDBD_25533_ANC",
        "900005": "C1_SGBD_17794_PEN",
    })

    assert max_tp == {"900004": "n/a", "900005": "n/a"}


def test_unrecognised_comments_get_no_invented_max_tp(fresh_db):
    _, _, max_tp = history._comment_attribution_maps({
        "900007": "positionOrder",
        "900008": "[sl 4046.50]",
    })

    assert max_tp == {}
