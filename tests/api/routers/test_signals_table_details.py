"""The Trading tab's Signals table, which showed almost nothing.

Reported by the owner, 2026-09-21: "trading > signals - needs populating with
details".

Same root cause as the Positions table the same day. The rows are raw
`vantage_signals` columns -- `source_name`, `entry_low`, `entry_high`,
`stop_loss`, `lot_size` -- and the browser was reading `source` and `entry`,
neither of which is a column. Every one of those cells rendered an em dash, so
the table showed a direction and a status and nothing else.

Fixed the way the Positions table was: a response model that fills the short
names FROM the real columns. Copied, never moved -- `get_signals` feeds the
signal editor and the AI commentary panel, which already read the long names,
and a validation alias would consume the original and break both to fix this.

A signal carries an entry RANGE, not a price. `entry` is the low end, which is
the level the range is quoted from, and both ends travel so the table can show
the band rather than implying a single number.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import trading as trading_router

ROW = {
    "signal_id": "sig-1", "source_name": "GoldSignals", "direction": "BUY",
    "entry_low": 4370.0, "entry_high": 4375.0, "stop_loss": 4350.0,
    "tp1": 4390.0, "tp2": 4400.0, "tp3": None,
    "lot_size": 0.1, "risk_pct": 1.0, "notes": "second message",
    "status": "pending", "created_at": 1_800_000_000.0,
    "activated_at": None, "cancelled_at": None, "claude_commentary": None,
}


@pytest.fixture
def signals(monkeypatch):
    state = {"rows": [dict(ROW)]}

    async def _get(engine, status=None):
        state["asked"] = status
        return [dict(r) for r in state["rows"]]

    monkeypatch.setattr(trading_router.trading_ctl, "get_signals", _get)
    return state


def _first(client):
    return client.get("/api/trading/signals").json()[0]


class TestTheColumnsThatWereDashes:
    def test_the_source(self, make_client, signals):
        assert _first(make_client())["source"] == "GoldSignals"

    def test_the_entry(self, make_client, signals):
        assert _first(make_client())["entry"] == 4370.0

    def test_the_far_end_of_the_entry_band(self, make_client, signals):
        """A signal is a range. Showing only one end implies a precision the
        signal does not have."""
        assert _first(make_client())["entry_high"] == 4375.0

    def test_the_stop(self, make_client, signals):
        assert _first(make_client())["sl"] == 4350.0

    def test_the_first_take_profit(self, make_client, signals):
        assert _first(make_client())["tp"] == 4390.0

    def test_the_lot_size(self, make_client, signals):
        assert _first(make_client())["lots"] == 0.1

    def test_the_id_the_editor_writes_back_to(self, make_client, signals):
        assert _first(make_client())["id"] == "sig-1"


class TestWhatTheRawColumnsStillAnswer:
    def test_the_long_names_are_kept_not_renamed(self, make_client, signals):
        """The signal editor and the commentary panel read these. A rename
        here would fix one screen by breaking two."""
        body = _first(make_client())
        assert body["source_name"] == "GoldSignals"
        assert body["entry_low"] == 4370.0
        assert body["stop_loss"] == 4350.0

    def test_a_caller_that_already_speaks_short_names_is_believed(
        self, make_client, signals,
    ):
        signals["rows"] = [dict(ROW, entry=4371.5)]

        assert _first(make_client())["entry"] == 4371.5


class TestAbsentIsAbsent:
    def test_a_missing_take_profit_stays_null(self, make_client, signals):
        """Not 0.0. A take-profit of zero is a level, and this table is read
        before a position is opened from it."""
        signals["rows"] = [dict(ROW, tp1=None)]

        assert _first(make_client())["tp"] is None

    def test_a_row_missing_everything_does_not_take_the_table_down(
        self, make_client, signals,
    ):
        signals["rows"] = [{"signal_id": "sig-2", "status": "pending"}]

        assert _first(make_client())["id"] == "sig-2"
