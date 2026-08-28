"""The cross-engine signal bus.

Each engine writes here when it generates a signal; every engine reads here at
ML-scoring time. Two consumers, and the second one can cost money:

  * get_concurrent_agreement() is an ML FEATURE. A wrong sign trains and scores
    against the opposite of reality.
  * has_conflict_on_bus() is a SUPPRESSION gate. False when it should be True
    lets two engines take opposite sides of the same market at once.

Both derive from get_concurrent_signals(), whose WHERE clause carries four
conditions that each exclude a different kind of stale row. The tests below
take them one at a time, because a row wrongly included is a phantom
disagreement and a row wrongly excluded is a missed conflict.

Runs against a real sqlite database via fresh_db -- the SQL is the behaviour
here, so faking the connection would test nothing.
"""
from __future__ import annotations

import time

import pytest

from backend.src.services.cluster import signal_bus_repo as bus


pytestmark = pytest.mark.usefixtures("fresh_db")


def _rows():
    from backend.src.db.database import db
    with db() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM signal_bus")]


class TestWrite:
    def test_it_creates_the_table_on_first_use(self):
        """Every entry point calls _ensure_signal_bus(). The bus was added
        after the initial schema, so an older database arrives without it."""
        row_id = bus.write_signal_bus("breakout", "BUY")
        assert row_id > 0
        assert len(_rows()) == 1

    def test_direction_is_normalised_to_upper_case(self):
        """Every comparison downstream is against an upper-case string. A
        lower-case row would never match an agreement and would always read as
        a disagreement."""
        bus.write_signal_bus("breakout", "buy")
        assert _rows()[0]["direction"] == "BUY"

    def test_it_records_the_engine_confidence_and_signal_id(self):
        bus.write_signal_bus("reversal", "SELL", confidence=0.75, signal_id=42)
        row = _rows()[0]
        assert row["engine"] == "reversal"
        assert row["confidence"] == 0.75
        assert row["signal_id"] == 42
        assert row["is_still_open"] == 1

    def test_the_default_ttl_is_five_minutes(self):
        """Matches the longest scalp trade lifetime. A longer default leaves
        finished signals suppressing new ones."""
        before = time.time()
        bus.write_signal_bus("breakout", "BUY")
        row = _rows()[0]
        assert 299 <= row["expires_at"] - before <= 301

    def test_a_write_failure_returns_zero_rather_than_raising(self, monkeypatch):
        """The bus is advisory. A broken bus must not stop an engine from
        generating its signal."""
        def _boom():
            raise RuntimeError("database is locked")
        monkeypatch.setattr(bus, "_ensure_signal_bus", _boom)
        assert bus.write_signal_bus("breakout", "BUY") == 0


class TestWhichRowsAreVisible:
    """One condition of the WHERE clause per test."""

    def test_another_engines_open_signal_is_visible(self):
        bus.write_signal_bus("breakout", "BUY")
        got = bus.get_concurrent_signals(exclude_engine="reversal")
        assert [s["engine"] for s in got] == ["breakout"]

    def test_the_callers_OWN_signal_is_excluded(self):
        """Otherwise every engine disagrees with itself the moment it writes,
        and has_conflict_on_bus() blocks the signal that just created it."""
        bus.write_signal_bus("breakout", "BUY")
        assert bus.get_concurrent_signals(exclude_engine="breakout") == []

    def test_an_expired_row_is_excluded(self):
        bus.write_signal_bus("breakout", "BUY", ttl_seconds=-1)
        assert bus.get_concurrent_signals(exclude_engine="reversal") == []

    def test_a_row_outside_the_window_is_excluded(self):
        """expires_at and the window are separate limits. A long TTL does not
        make an old signal current."""
        bus.write_signal_bus("breakout", "BUY", ttl_seconds=10_000)
        assert bus.get_concurrent_signals("reversal", window_seconds=10_000) != []
        assert bus.get_concurrent_signals("reversal", window_seconds=-1) == []

    def test_a_CLOSED_row_is_excluded_even_though_its_ttl_has_not_expired(self):
        """The reason close_bus_entry() exists. Without it a signal that hit
        SL keeps suppressing the other engine for the rest of its TTL."""
        bus.write_signal_bus("breakout", "BUY", ttl_seconds=300, signal_id=7)
        assert bus.get_concurrent_signals("reversal") != []

        bus.close_bus_entry("breakout", 7)

        assert bus.get_concurrent_signals("reversal") == []

    def test_close_bus_entry_closes_only_that_engines_row(self):
        """signal_id is per-engine and not globally unique. Matching on it
        alone would close another engine's live signal."""
        bus.write_signal_bus("breakout", "BUY", signal_id=7)
        bus.write_signal_bus("reversal", "SELL", signal_id=7)

        bus.close_bus_entry("breakout", 7)

        open_rows = {r["engine"]: r["is_still_open"] for r in _rows()}
        assert open_rows == {"breakout": 0, "reversal": 1}

    def test_a_read_failure_returns_empty_rather_than_raising(self, monkeypatch):
        def _boom():
            raise RuntimeError("database is locked")
        monkeypatch.setattr(bus, "_ensure_signal_bus", _boom)
        assert bus.get_concurrent_signals("reversal") == []


class TestConcurrentAgreement:
    """An ML feature. The sign is the whole value."""

    def test_no_other_signals_is_neutral(self):
        assert bus.get_concurrent_agreement("breakout", "BUY") == 0.0

    def test_another_engine_in_the_same_direction_agrees(self):
        bus.write_signal_bus("reversal", "BUY")
        assert bus.get_concurrent_agreement("breakout", "BUY") == 1.0

    def test_another_engine_in_the_opposite_direction_disagrees(self):
        bus.write_signal_bus("reversal", "SELL")
        assert bus.get_concurrent_agreement("breakout", "BUY") == -1.0

    def test_DISAGREEMENT_WINS_when_both_are_present(self):
        """Documented precedence, and the conservative one: one engine taking
        the other side matters more than another agreeing."""
        bus.write_signal_bus("reversal", "BUY")
        bus.write_signal_bus("scalp", "SELL")
        assert bus.get_concurrent_agreement("breakout", "BUY") == -1.0

    def test_the_callers_direction_is_upper_cased_before_comparing(self):
        """Stored rows are upper-case. A lower-case argument compared raw
        matches nothing, so agreement silently reads as disagreement."""
        bus.write_signal_bus("reversal", "BUY")
        assert bus.get_concurrent_agreement("breakout", "buy") == 1.0

    def test_the_engines_own_signal_does_not_make_it_agree_with_itself(self):
        bus.write_signal_bus("breakout", "BUY")
        assert bus.get_concurrent_agreement("breakout", "BUY") == 0.0


class TestConflictGate:
    """Suppression. False when it should be True puts two engines on opposite
    sides of the same market."""

    def test_an_opposite_signal_is_a_conflict(self):
        bus.write_signal_bus("reversal", "SELL")
        assert bus.has_conflict_on_bus("breakout", "BUY") is True

    def test_an_agreeing_signal_is_not(self):
        bus.write_signal_bus("reversal", "BUY")
        assert bus.has_conflict_on_bus("breakout", "BUY") is False

    def test_an_empty_bus_is_not(self):
        assert bus.has_conflict_on_bus("breakout", "BUY") is False

    def test_a_closed_opposite_signal_is_not(self):
        bus.write_signal_bus("reversal", "SELL", signal_id=3)
        bus.close_bus_entry("reversal", 3)
        assert bus.has_conflict_on_bus("breakout", "BUY") is False

    def test_its_default_window_is_TEN_minutes_not_the_readers_fifteen(self):
        """get_concurrent_signals defaults to 900s; this gate passes 600. The
        difference is deliberate -- suppression uses a tighter window than the
        ML feature -- and is easy to lose by dropping the argument."""
        bus.write_signal_bus("reversal", "SELL", ttl_seconds=10_000)
        from backend.src.db.database import db
        with db() as conn:      # backdate it to 12 minutes ago
            conn.execute("UPDATE signal_bus SET created_at = ?",
                         (time.time() - 720,))

        assert bus.has_conflict_on_bus("breakout", "BUY") is False, \
            "the 600s suppression window was widened"
        assert bus.get_concurrent_agreement("breakout", "BUY") == -1.0, \
            "the 900s feature window was narrowed"


class TestPrune:
    def test_it_deletes_expired_rows_and_keeps_live_ones(self):
        bus.write_signal_bus("breakout", "BUY", ttl_seconds=-1)
        bus.write_signal_bus("reversal", "SELL", ttl_seconds=300)

        bus.prune_signal_bus()

        assert [r["engine"] for r in _rows()] == ["reversal"]

    def test_it_keeps_a_CLOSED_but_unexpired_row(self):
        """Pruning is by TTL only. Deleting closed rows early would be a
        different policy, and nothing else deletes them."""
        bus.write_signal_bus("breakout", "BUY", ttl_seconds=300, signal_id=1)
        bus.close_bus_entry("breakout", 1)

        bus.prune_signal_bus()

        assert len(_rows()) == 1
