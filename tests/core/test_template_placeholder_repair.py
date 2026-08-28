"""Polling self-heal for EA Template placeholder rows (mt5_ticket=0,
entry_price=0) whose leg-fill event never reached this node -- the same
$0-entry ghost the user hit twice: trade eb8ca404 (2026-07-28) and
c2ebb432 (2026-07-29, its anchor leg opened AND closed at the broker while
every leg event was being discarded as an "unknown trade_id").

Legs are matched by the comment the EA stamps on each one,
"ea:<first 10 chars of trade_id><a|g><N>".
"""
import asyncio
import time
from unittest import mock


from backend.src.services.positions import core_template_placeholder_repair as repair
from backend.src.db import database as db


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


TRADE_ID = "c2ebb432-8def-41"   # first 10 chars -> "c2ebb432-8", as the EA slices it


class _FakeBridge:
    def __init__(self, positions=None, deals=None):
        self._positions = positions if positions is not None else []
        self._deals = deals or []

    def is_configured(self):
        return True

    async def get_positions(self):
        return self._positions

    async def get_deal_history(self, days):
        return self._deals

    async def get_account(self):
        return {"balance": 599.59, "equity": 599.59, "margin_free": 538.70}

    async def close_position(self, ticket):
        raise AssertionError("repair must never place or close a broker order")


def _insert_placeholder(trade_id=TRADE_ID, entry_price=0.0, mt5_ticket=0, age_s=0.0):
    now = time.time() - age_s
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,entry_high,"
            "stop_loss,lot_size,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", "Reversal Engine", "BUY", 4015.0, 4018.0, 4011.5, 0.04,
             "active", now),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id,signal_id,mt5_ticket,direction,"
            "entry_low,entry_high,entry_price,lot_size,remaining_lots,stop_loss,status,open_time,"
            "strategy,managed_by,tg_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", mt5_ticket, "BUY", 4015.0, 4018.0, entry_price,
             0.04, 0.04, 4011.5, "open", now, "template:Sig Gen Grid", "ea", "Reversal Engine"),
        )


def _row(trade_id=TRADE_ID):
    with db.db() as conn:
        return db.row_to_dict(conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone())


def test_adopts_placeholder_onto_still_open_leg_position(fresh_db):
    _insert_placeholder()
    bridge = _FakeBridge(positions=[{
        "ticket": 1399609711, "volume": 0.03, "open_price": 4015.46,
        "comment": "ea:c2ebb432-8a1", "type": "BUY",
    }])
    n = asyncio.run(repair.repair_template_placeholders(bridge))
    assert n == 1
    row = _row()
    assert row["mt5_ticket"] == 1399609711
    assert row["entry_price"] == 4015.46
    assert row["lot_size"] == 0.03          # the EA's own anchor lot, not Python's sizing
    assert row["remaining_lots"] == 0.03
    assert row["status"] == "open"


def test_closes_placeholder_from_broker_deal_history(fresh_db):
    """The real c2ebb432 case: the anchor opened at 4015.46 and closed at
    4035.50 for +$60.12 while every leg event was being dropped, leaving the
    row open at a $0 entry indefinitely."""
    _insert_placeholder()
    bridge = _FakeBridge(positions=[], deals=[
        {"ticket": 1399609711, "order": 1672649002, "position_id": 1672649002,
         "entry": 0, "type": 0, "volume": 0.03, "price": 4015.46, "profit": 0.0,
         "swap": 0.0, "fee": 0.0, "time": 1785353027, "comment": "ea:c2ebb432-8a1"},
        {"ticket": 1399806615, "order": 1672926348, "position_id": 1672649002,
         "entry": 1, "type": 1, "volume": 0.03, "price": 4035.5, "profit": 60.12,
         "swap": 0.0, "fee": 0.0, "time": 1785355457, "comment": ""},
    ])
    sent = []

    async def _capture(msg, *a, **k):
        sent.append(msg)

    with mock.patch("backend.src.services.telegram.alerts.send_message", side_effect=_capture):
        n = asyncio.run(repair.repair_template_placeholders(bridge))
        asyncio.run(asyncio.sleep(0))

    assert n == 1
    row = _row()
    assert row["status"] == "closed"
    assert row["entry_price"] == 4015.46
    assert row["close_price"] == 4035.5
    assert row["mt5_profit"] == 60.12
    assert len(sent) == 1
    assert "4015.46" in sent[0]     # real entry quoted, never "$0.00"
    assert "60.12" in sent[0]


def test_leaves_placeholder_alone_when_no_leg_has_filled(fresh_db):
    """Legs may still be resting as pending orders -- nothing to repair, and
    certainly nothing to close."""
    _insert_placeholder()
    bridge = _FakeBridge(positions=[], deals=[])
    assert asyncio.run(repair.repair_template_placeholders(bridge)) == 0
    assert _row()["status"] == "open"


def test_ignores_rows_that_already_have_a_real_entry_price(fresh_db):
    """A ticket-less row WITH an entry price is a legitimately simulated
    trade, not an unpromoted template placeholder."""
    _insert_placeholder(entry_price=4015.0)
    bridge = _FakeBridge(positions=[{
        "ticket": 1, "volume": 0.03, "open_price": 4000.0,
        "comment": "ea:c2ebb432-8a1", "type": "BUY",
    }])
    assert asyncio.run(repair.repair_template_placeholders(bridge)) == 0
    assert _row()["mt5_ticket"] == 0


def test_does_not_match_another_trades_leg_comment(fresh_db):
    _insert_placeholder()
    bridge = _FakeBridge(positions=[{
        "ticket": 42, "volume": 0.03, "open_price": 4000.0,
        "comment": "ea:99999999-0a1", "type": "BUY",
    }])
    assert asyncio.run(repair.repair_template_placeholders(bridge)) == 0
    assert _row()["mt5_ticket"] == 0


# ── bugs/016: a placeholder nothing will ever fill ───────────────────────────
#
# Row 83aa3510 sat status='open' with mt5_ticket=0 for 26 hours on the live
# demo account while the broker had no position and zero margin. It held one of
# five trade slots the whole time, because open_trade()'s max-open-trades gate
# is a plain COUNT(*) of open rows and never asks the broker.
#
# It was stuck in a state neither event-driven path can leave:
#   * grid_legs_total IS NULL -- the EA's open ack never arrived, and
#     _on_grid_leg_cancelled's expiry requires `total is not None`, so the
#     existing no_fill_expired close could never fire for it;
#   * mt5_ticket=0 -- no fill event arrived either, so _promote_leg_fill never
#     ran.
# and the polling repair below deliberately declined to act, because it cannot
# tell "legs still resting as pending orders" from "legs never existed".
#
# Age is what tells them apart. These tests pin that, and the two directions
# that must NOT change: a young placeholder is still left alone (see
# test_leaves_placeholder_alone_when_no_leg_has_filled above, whose fixture
# inserts open_time=now), and a placeholder with any broker evidence is still
# adopted or closed from that evidence rather than expired.


def _dead_bridge():
    """No live legs, no deals -- the broker has never heard of this trade."""
    return _FakeBridge(positions=[], deals=[])


def test_closes_a_placeholder_that_is_long_dead(fresh_db):
    """The bugs/016 row. No live leg, no opening deal, and older than the
    expiry -- nothing is coming. Left open it costs a trade slot forever."""
    _insert_placeholder(age_s=repair.placeholder_no_fill_expiry_secs() + 60)

    assert asyncio.run(repair.repair_template_placeholders(_dead_bridge())) == 1

    row = _row()
    assert row["status"] == "closed"
    assert row["exit_reason"] == "no_fill_expired"


def test_the_expired_placeholder_books_NO_PROFIT_OR_LOSS(fresh_db):
    """It never filled, so there is nothing to book. record_close's own
    entry_price==0 guard is what stops a $0 entry being turned into a P&L the
    size of the contract -- a real -$15.63 was once reported as -$16,086 that
    way (trade 76687f1a, 2026-07-29)."""
    _insert_placeholder(age_s=repair.placeholder_no_fill_expiry_secs() + 60)

    asyncio.run(repair.repair_template_placeholders(_dead_bridge()))

    row = _row()
    assert float(row["net_pnl"] or 0) == 0.0
    assert float(row["realised_pnl"] or 0) == 0.0
    # The recorded exit price matters too, even though record_close ignores it
    # for P&L on a zero-entry row: it is what History shows. A price here would
    # display a trade that never opened as having closed at a real level.
    # Found by mutation -- 4015.0 passed every other assertion in this file.
    assert float(row["close_price"] or 0) == 0.0


def test_it_frees_the_trade_slot(fresh_db):
    """The actual harm in 016, stated as the thing the user cares about."""
    from backend.src.services.trading import trade_repo

    _insert_placeholder(age_s=repair.placeholder_no_fill_expiry_secs() + 60)
    assert trade_repo.count_open_trades() == 1

    asyncio.run(repair.repair_template_placeholders(_dead_bridge()))

    assert trade_repo.count_open_trades() == 0


def test_a_placeholder_just_UNDER_the_expiry_is_left_alone(fresh_db):
    """The boundary that protects a genuinely resting pending order. Expiring
    one early would close a trade that is still about to fill."""
    _insert_placeholder(age_s=repair.placeholder_no_fill_expiry_secs() - 60)

    assert asyncio.run(repair.repair_template_placeholders(_dead_bridge())) == 0
    assert _row()["status"] == "open"


def test_a_LIVE_LEG_still_wins_over_the_expiry(fresh_db):
    """Age must never override broker evidence. An old placeholder whose leg
    is open at the broker is a real position -- expiring it would abandon a
    live trade instead of adopting it."""
    _insert_placeholder(age_s=repair.placeholder_no_fill_expiry_secs() + 86400)
    bridge = _FakeBridge(positions=[{
        "ticket": 1399609711, "volume": 0.03, "open_price": 4015.46,
        "comment": "ea:c2ebb432-8a1", "type": "BUY",
    }])

    assert asyncio.run(repair.repair_template_placeholders(bridge)) == 1

    row = _row()
    assert row["status"] == "open", "a live position was expired"
    assert int(row["mt5_ticket"]) == 1399609711


def test_an_OPENING_DEAL_still_wins_over_the_expiry(fresh_db):
    """Same rule from the other side: if the leg opened and closed, the close
    must be recorded from the broker's own numbers, not booked as never
    filled at zero."""
    _insert_placeholder(age_s=repair.placeholder_no_fill_expiry_secs() + 86400)
    bridge = _FakeBridge(positions=[], deals=[
        {"position_id": 55, "order": 1399609711, "comment": "ea:c2ebb432-8a1",
         "entry": 0, "price": 4015.46, "volume": 0.03, "time": 1000},
        {"position_id": 55, "entry": 1, "price": 4020.00, "volume": 0.03,
         "profit": 13.62, "swap": 0.0, "fee": 0.0, "time": 2000,
         "comment": "tp"},
    ])

    assert asyncio.run(repair.repair_template_placeholders(bridge)) == 1

    row = _row()
    assert row["status"] == "closed"
    assert row["exit_reason"] != "no_fill_expired"
    assert float(row["close_price"]) == 4020.00


def test_expiring_NEVER_TOUCHES_THE_BROKER(fresh_db):
    """It is an app-side row for a position that does not exist. _FakeBridge's
    close_position raises on sight, so reaching it fails this test."""
    _insert_placeholder(age_s=repair.placeholder_no_fill_expiry_secs() + 60)

    asyncio.run(repair.repair_template_placeholders(_dead_bridge()))   # must not raise

    assert _row()["status"] == "closed"


def test_a_row_with_a_real_entry_price_is_never_expired(fresh_db):
    """Age applies only to the placeholder signature. A ticket-less row WITH
    an entry price is a legitimately simulated trade and is not this bug."""
    _insert_placeholder(entry_price=4015.0,
                        age_s=repair.placeholder_no_fill_expiry_secs() + 86400)

    assert asyncio.run(repair.repair_template_placeholders(_dead_bridge())) == 0
    assert _row()["status"] == "open"


def test_the_default_expiry_is_still_24_hours(fresh_db):
    """Upgrade safety -- the value must not drift silently."""
    assert repair.placeholder_no_fill_expiry_secs() == 86400


def test_the_expiry_follows_the_tunable(fresh_db):
    """And the control is actually wired to the behaviour, not just present."""
    from backend.src.services.risk import expert_params as ep

    ep.set_params({"placeholder_no_fill_expiry_s": 3600})
    try:
        _insert_placeholder(age_s=7200)
        assert asyncio.run(repair.repair_template_placeholders(_dead_bridge())) == 1
        assert _row()["exit_reason"] == "no_fill_expired"
    finally:
        ep.set_params({"placeholder_no_fill_expiry_s": 86400})
