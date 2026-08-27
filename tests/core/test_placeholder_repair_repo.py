"""The template-placeholder repair SQL, moved into its own repo.

A "placeholder" is a row this app wrote for an EA Template leg whose fill
event never arrived: status open, no ticket, entry_price 0. Repair either
adopts it onto a live leg found at the broker, or records the real fill before
closing it. Both write the numbers a P&L is later computed from, so the
selection criteria and the guard on the adopt are pinned here.
"""
from __future__ import annotations

import pytest

from backend.src.db import database as db
from backend.src.services.positions import repair_repo


def _trade(conn, trade_id, **over):
    conn.execute(
        "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
        "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
        ("s-" + trade_id, "BUY", 4000.0, 4001.0, 3990.0, "active", 1700000000.0))
    row = {
        "trade_id": trade_id, "signal_id": "s-" + trade_id, "direction": "BUY",
        "entry_low": 0.0, "entry_high": 0.0, "entry_price": 0.0,
        "lot_size": 0.0, "remaining_lots": 0.0, "stop_loss": 3990.0,
        "open_time": 1700000000.0, "status": "open", "mt5_ticket": None,
    }
    row.update(over)
    conn.execute(
        f"INSERT INTO vantage_simulated_trades ({', '.join(row)}) "
        f"VALUES ({', '.join('?' * len(row))})", tuple(row.values()))


def _row(trade_id="t1", cols="*"):
    with db.db() as conn:
        r = conn.execute(
            f"SELECT {cols} FROM vantage_simulated_trades WHERE trade_id=?",
            (trade_id,)).fetchone()
    return dict(r) if r is not None else None


class TestFetchTemplatePlaceholders:
    def test_finds_the_defect_signature(self, fresh_db):
        with fresh_db.db() as conn:
            _trade(conn, "t1")
        assert [r["trade_id"] for r in repair_repo.fetch_template_placeholders()] == ["t1"]

    def test_a_zero_ticket_counts_the_same_as_a_null_one(self, fresh_db):
        with fresh_db.db() as conn:
            _trade(conn, "t1", mt5_ticket=0)
        assert len(repair_repo.fetch_template_placeholders()) == 1

    @pytest.mark.parametrize("why, over", [
        ("closed",     {"status": "closed"}),
        # The distinguishing half of the signature. A row with no ticket but a
        # real entry price is a legitimate ticket-less simulated trade --
        # adopting it onto a broker leg would rewrite a real entry.
        ("real entry", {"entry_price": 4000.5}),
        ("has ticket", {"mt5_ticket": 999}),
    ])
    def test_excludes(self, fresh_db, why, over):
        with fresh_db.db() as conn:
            _trade(conn, "t1", **over)
        assert repair_repo.fetch_template_placeholders() == [], why


class TestAdoptPlaceholderOntoLeg:
    def test_writes_the_fill_across_every_price_and_size_column(self, fresh_db):
        with fresh_db.db() as conn:
            _trade(conn, "t1")
        repair_repo.adopt_placeholder_onto_leg("t1", 4242, 4000.75, 0.05)
        r = _row()
        assert (r["mt5_ticket"], r["entry_price"], r["entry_low"], r["entry_high"],
                r["lot_size"], r["remaining_lots"]) == (4242, 4000.75, 4000.75,
                                                        4000.75, 0.05, 0.05)

    @pytest.mark.parametrize("why, over", [
        # The row is only a placeholder while it has no ticket. If a fill event
        # landed between the scan and this write, the ticket already there is
        # the real one -- overwriting it points the row at the wrong position.
        ("already ticketed", {"mt5_ticket": 999}),
        ("already closed",   {"status": "closed"}),
    ])
    def test_the_where_guard_refuses_a_row_that_moved_on(self, fresh_db, why, over):
        with fresh_db.db() as conn:
            _trade(conn, "t1", **over)
        repair_repo.adopt_placeholder_onto_leg("t1", 4242, 4000.75, 0.05)
        assert _row()["entry_price"] == 0.0, why

    def test_only_the_named_trade(self, fresh_db):
        with fresh_db.db() as conn:
            _trade(conn, "t1")
            _trade(conn, "t2")
        repair_repo.adopt_placeholder_onto_leg("t1", 4242, 4000.75, 0.05)
        assert _row("t2")["entry_price"] == 0.0


class TestRecordPlaceholderFill:
    def test_writes_the_fill_and_the_brokers_profit(self, fresh_db):
        """Written before record_close so the close computes against a genuine
        entry price rather than fabricating a P&L from a zero entry."""
        with fresh_db.db() as conn:
            _trade(conn, "t1")
        repair_repo.record_placeholder_fill("t1", 4242, 4000.75, 0.05, 0.05, -12.34)
        r = _row()
        assert (r["mt5_ticket"], r["entry_price"], r["entry_low"], r["entry_high"],
                r["lot_size"], r["remaining_lots"], r["mt5_profit"]) == (
            4242, 4000.75, 4000.75, 4000.75, 0.05, 0.05, -12.34)

    def test_it_is_unguarded_unlike_the_adopt(self, fresh_db):
        """Deliberate: this runs on a row the caller has already resolved and
        is about to close, so a ticket appearing meanwhile must not silently
        skip the write and leave record_close computing off a zero entry."""
        with fresh_db.db() as conn:
            _trade(conn, "t1", mt5_ticket=999)
        repair_repo.record_placeholder_fill("t1", 4242, 4000.75, 0.05, 0.05, -12.34)
        assert _row()["entry_price"] == 4000.75

    def test_lot_size_and_remaining_lots_are_written_independently(self, fresh_db):
        """The caller falls back to a different column for each when the
        broker reports no volume, so collapsing them into one `lots` would
        quietly equalise a partially-closed row."""
        with fresh_db.db() as conn:
            _trade(conn, "t1")
        repair_repo.record_placeholder_fill("t1", 4242, 4000.75, 0.10, 0.04, -12.34)
        r = _row()
        assert (r["lot_size"], r["remaining_lots"]) == (0.10, 0.04)

    def test_only_the_named_trade(self, fresh_db):
        with fresh_db.db() as conn:
            _trade(conn, "t1")
            _trade(conn, "t2")
        repair_repo.record_placeholder_fill("t1", 4242, 4000.75, 0.05, 0.05, -12.34)
        assert _row("t2")["entry_price"] == 0.0
