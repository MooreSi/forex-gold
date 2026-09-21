"""An open position says what opened it and how it is being managed.

The owner asked for the source and the strategy on the Positions table
(2026-09-21). The rows have always carried them, but not in a form anyone
should have to read: `strategy` is stored as `template:<name>` and `tg_source`
holds an engine name, a Telegram channel name, or a marker like
`manual_market`.

The labels are decided here rather than in the browser for the same reason the
EA badge's colour is: re-deriving the rules in TypeScript would be a second
answer to what a trade's source is, and the two would drift.

Added alongside the raw columns, never replacing them -- several screens read
`strategy` and `tg_source` directly.
"""
from __future__ import annotations

import pytest

from backend.src.db import database as db_module
from backend.src.services.analytics import reporting


def _insert(conn, **over):
    # The trade's signal_id is a foreign key into vantage_signals, so the
    # signal has to exist first -- same shape the reversal-engine tests use.
    conn.execute(
        "INSERT INTO vantage_signals "
        "(signal_id, direction, entry_low, entry_high, stop_loss, created_at) "
        "VALUES (?,?,?,?,?,0)", ("s1", "SELL", 4376.0, 4378.0, 4426.97))
    row = {
        "trade_id": "t-display-1", "signal_id": "s1", "direction": "SELL",
        "entry_price": 4376.97, "lot_size": 0.1, "stop_loss": 4426.97,
        "status": "open", "strategy": "template:30 TP1 SL50 and Trail",
        "tg_source": "Reversal Engine", "open_time": 1789000000.0,
        "entry_low": 4376.0, "entry_high": 4378.0, "remaining_lots": 0.1,
    }
    row.update(over)
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(
        f"INSERT INTO vantage_simulated_trades ({cols}) VALUES ({marks})",
        list(row.values()),
    )


@pytest.fixture
def one_open_trade(fresh_db):
    def _make(**over):
        with db_module.db() as conn:
            conn.execute("DELETE FROM vantage_simulated_trades")
            _insert(conn, **over)
        return reporting.get_open_trades()[0]
    return _make


class TestTheSource:
    def test_an_engine_is_named(self, one_open_trade):
        assert one_open_trade()["source_label"] == "Reversal Engine"

    def test_a_channel_is_named(self, one_open_trade):
        assert one_open_trade(tg_source="Gold Diggers VIP")["source_label"] \
            == "Gold Diggers VIP"

    def test_a_manual_order_reads_as_one(self, one_open_trade):
        """`manual_market` is a marker, not something to show an operator."""
        assert one_open_trade(tg_source="manual_market")["source_label"] \
            == "Manual Market"

    def test_a_trade_with_no_source_still_says_something(self, one_open_trade):
        assert one_open_trade(tg_source="")["source_label"] == "Manual Signal"


class TestTheStrategy:
    def test_a_template_is_named_in_words(self, one_open_trade):
        assert one_open_trade()["strategy_label"] == "Template: 30 TP1 SL50 and Trail"

    def test_a_built_in_strategy_is_named(self, one_open_trade):
        assert one_open_trade(strategy="scale_out")["strategy_label"] not in ("", None)

    def test_no_strategy_renders_a_dash_not_a_blank(self, one_open_trade):
        """A blank cell reads as a bug; an em dash reads as 'none'."""
        assert one_open_trade(strategy="")["strategy_label"] == "—"


def test_the_raw_columns_are_untouched(one_open_trade):
    """Other screens read these. The labels are additions."""
    row = one_open_trade()

    assert row["strategy"] == "template:30 TP1 SL50 and Trail"
    assert row["tg_source"] == "Reversal Engine"
