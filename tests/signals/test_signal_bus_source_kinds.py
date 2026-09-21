"""The signal bus gains a source kind, a symbol and a source name.

Stage 1 of contradiction handling. Telegram signals have never reached the
bus at all, so channel-vs-engine and channel-vs-channel contradictions were
invisible. Putting them on the bus is only safe if the two existing readers
cannot see them: `has_conflict_on_bus` is a live suppression gate, and the
breakout engine reads it over a SIX HOUR window. A Telegram row becoming
visible there would silently suppress engine signals that fire today.

So the contract these tests pin is deliberately lopsided:

  * every reader defaults to engine rows only, exactly as before;
  * a row written before this change (source_kind NULL) still counts as an
    engine row, because that is what it was;
  * a caller that wants the whole bus has to say so.

The symbol column is the same shape of problem. The bus never had one, so a
BUY on one instrument already counted against a SELL on another. Filtering
is opt-in for the same reason, and an unknown symbol matches everything --
only legacy rows have one, and a row whose instrument is unknown cannot be
ruled out.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster import signal_bus_repo as bus


pytestmark = pytest.mark.usefixtures("fresh_db")


def _rows():
    from backend.src.db.database import db
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM signal_bus")]


class TestWhatAWriteRecords:
    def test_it_stores_the_symbol_kind_and_source_name(self):
        bus.write_signal_bus(
            "telegram", "BUY", symbol="XAUUSD",
            source_kind=bus.KIND_TELEGRAM, source_name="Gold Diggers VIP",
        )
        row = _rows()[0]
        assert row["symbol"] == "XAUUSD"
        assert row["source_kind"] == bus.KIND_TELEGRAM
        assert row["source_name"] == "Gold Diggers VIP"

    def test_a_write_that_says_nothing_is_an_engine_row(self):
        """Every existing call site omits all three. Defaulting to anything
        else would move those rows out from under the live readers."""
        bus.write_signal_bus("breakout", "BUY")
        row = _rows()[0]
        assert row["source_kind"] == bus.KIND_ENGINE
        assert row["source_name"] == "breakout"
        assert row["symbol"] is None


class TestTelegramRowsAreInvisibleToTheLiveReaders:
    """The whole safety argument for stage 1. Each test has its negative
    control immediately after it -- a filter that excludes everything would
    pass the first assertion of each pair for the wrong reason."""

    def _a_telegram_sell(self):
        bus.write_signal_bus("telegram", "SELL", symbol="XAUUSD",
                             source_kind=bus.KIND_TELEGRAM,
                             source_name="Gold Diggers VIP")

    def test_get_concurrent_signals_does_not_see_it(self):
        self._a_telegram_sell()
        assert bus.get_concurrent_signals(exclude_engine="breakout") == []

    def test_but_it_is_there_when_the_caller_asks_for_it(self):
        self._a_telegram_sell()
        got = bus.get_concurrent_signals(
            exclude_engine="breakout", kinds=(bus.KIND_ENGINE, bus.KIND_TELEGRAM))
        assert [s["source_name"] for s in got] == ["Gold Diggers VIP"]

    def test_has_conflict_on_bus_does_not_fire_on_it(self):
        self._a_telegram_sell()
        assert bus.has_conflict_on_bus("breakout", "BUY") is False

    def test_and_still_fires_on_an_engine_row(self):
        bus.write_signal_bus("reversal", "SELL")
        assert bus.has_conflict_on_bus("breakout", "BUY") is True

    def test_concurrent_agreement_ignores_it(self):
        """An ML feature. A Telegram row flipping its sign would retrain both
        engines on a fact they were never scored against."""
        self._a_telegram_sell()
        assert bus.get_concurrent_agreement("breakout", "BUY") == 0.0


class TestALegacyRowCountsAsAnEngineRow:
    def test_a_null_source_kind_is_still_visible_to_the_default_reader(self):
        """Every row written before this change has NULL here. Reading it as
        anything but 'engine' would empty the bus on upgrade -- and the
        emptying would look like peace, not like a broken gate."""
        bus.write_signal_bus("reversal", "SELL")
        from backend.src.db.database import db
        with db() as conn:
            conn.execute("UPDATE signal_bus SET source_kind = NULL")

        assert bus.has_conflict_on_bus("breakout", "BUY") is True


class TestSymbolFiltering:
    def test_a_row_on_another_instrument_is_excluded_when_a_symbol_is_given(self):
        bus.write_signal_bus("reversal", "SELL", symbol="EURUSD")
        assert bus.get_concurrent_signals("breakout", symbol="XAUUSD") == []

    def test_the_same_instrument_is_included(self):
        bus.write_signal_bus("reversal", "SELL", symbol="XAUUSD")
        assert len(bus.get_concurrent_signals("breakout", symbol="XAUUSD")) == 1

    def test_the_comparison_ignores_case(self):
        bus.write_signal_bus("reversal", "SELL", symbol="xauusd")
        assert len(bus.get_concurrent_signals("breakout", symbol="XAUUSD")) == 1

    def test_a_row_with_no_symbol_is_included_because_it_cannot_be_ruled_out(self):
        bus.write_signal_bus("reversal", "SELL")
        assert len(bus.get_concurrent_signals("breakout", symbol="XAUUSD")) == 1

    def test_asking_without_a_symbol_filters_nothing(self):
        """The default, and what every existing caller does."""
        bus.write_signal_bus("reversal", "SELL", symbol="EURUSD")
        assert len(bus.get_concurrent_signals("breakout")) == 1
