"""The contradiction study: recording, and the toggle that stops it.

This sits on the order path, so its contract is the decision log's, for the
same reasons:

  * **Off unless asked for.** With `tg_contradiction_log_enabled` off,
    nothing runs -- no bus read, no bus WRITE, no database work at all. The
    bus write matters as much as the read: with the toggle off the bus must
    look exactly as it did before this feature existed.
  * **It never raises.** A research log that can break signal processing is
    not worth having.
  * **An unavailable fact abstains.** Fill state is not on the bus yet, so
    `freshest wins` records NULL, not an allow.
"""
from __future__ import annotations

import os
import tempfile
import time

import pytest

from backend.src.services.signals import contradiction as c
from backend.src.services.signals import contradiction_log as clog
from backend.src.services.signals import contradiction_log_repo as repo
from backend.src.services.cluster import signal_bus_repo as bus
from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
from tests.conftest import remove_db_file


ON = {"tg_contradiction_log_enabled": 1}
OFF = {"tg_contradiction_log_enabled": 0}


@pytest.fixture
def study(fresh_db):
    """The core db (for the bus) plus a throwaway RE db (for the study)."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_repo.init(path)
    repo.create_schema()
    yield repo
    re_repo.close_db()
    remove_db_file(path)


def _note(rs=None, **over):
    kw = dict(source_kind=c.KIND_TELEGRAM, source_name="Gold Diggers VIP",
              direction="BUY", symbol="XAUUSD", candidate_ref="21429")
    kw.update(over)
    return clog.note_signal(rs if rs is not None else ON, **kw)


def _bus_rows():
    """No table and no rows are the same answer to 'what did this write'.
    The bus table is created lazily by the first write, so with the toggle
    off it is genuinely absent -- which is the strongest form of the thing
    being asserted, not a reason to weaken the assertion."""
    import sqlite3
    from backend.src.db.database import db
    with db() as conn:
        try:
            return [dict(r) for r in conn.execute("SELECT * FROM signal_bus")]
        except sqlite3.OperationalError:
            return []


class TestTheToggle:
    def test_with_it_off_nothing_is_recorded(self, study):
        _note(OFF)
        assert study.rows() == []

    def test_with_it_off_the_signal_never_reaches_the_bus_either(self, study):
        """The bus is read by a live suppression gate. An 'off' feature that
        still wrote rows to it would be off in name only."""
        _note(OFF)
        assert _bus_rows() == []

    def test_with_it_on_the_signal_is_recorded_and_reaches_the_bus(self, study):
        _note(ON)
        assert len(study.rows()) == 1
        assert len(_bus_rows()) == 1

    def test_a_missing_key_reads_as_off(self, study):
        _note({})
        assert study.rows() == []


class TestWhatIsRecorded:
    def test_a_signal_with_nothing_against_it_is_still_a_row(self, study):
        """The denominator. Without it every policy looks like it fires on
        everything it sees."""
        _note()
        row = study.rows()[0]
        assert row["opposing_count"] == 0
        assert row["source_name"] == "Gold Diggers VIP"
        assert row["direction"] == "BUY"

    def test_an_opposing_engine_signal_is_counted_and_named(self, study):
        bus.write_signal_bus("Reversal Engine", "SELL", symbol="XAUUSD",
                             source_kind=bus.KIND_ENGINE)
        _note()
        row = study.rows()[0]
        assert row["opposing_count"] == 1
        assert "Reversal Engine" in row["opposing_json"]

    def test_an_opposing_signal_from_ANOTHER_CHANNEL_is_counted(self, study):
        """The case that has never been visible anywhere in this system."""
        bus.write_signal_bus("Other Channel", "SELL", symbol="XAUUSD",
                             source_kind=bus.KIND_TELEGRAM)
        _note()
        assert study.rows()[0]["opposing_count"] == 1

    def test_the_channels_OWN_earlier_signal_is_not_counted_against_it(self, study):
        bus.write_signal_bus("Gold Diggers VIP", "SELL", symbol="XAUUSD",
                             source_kind=bus.KIND_TELEGRAM)
        _note()
        assert study.rows()[0]["opposing_count"] == 0

    def test_a_rescan_of_the_same_message_does_not_add_a_second_row(self, study):
        """The scan loop re-sees a message about once a second."""
        _note()
        _note()
        assert len(study.rows()) == 1


class TestTheVerdicts:
    def test_every_shipped_policy_gets_a_verdict(self, study):
        _note()
        got = study.verdicts(study.rows()[0]["id"])
        assert {v["policy"] for v in got} == {p.name for p in c.POLICIES}

    def test_first_wins_blocks_when_an_engine_opposes(self, study):
        bus.write_signal_bus("Reversal Engine", "SELL", symbol="XAUUSD",
                             source_kind=bus.KIND_ENGINE)
        _note()
        got = {v["policy"]: v["action"] for v in study.verdicts(study.rows()[0]["id"])}
        assert got["first wins"] == c.BLOCK
        assert got["live (champion)"] == c.ALLOW

    def test_freshest_wins_ABSTAINS_because_the_bus_has_no_fill_state(self, study):
        """NULL, not 'allow'. The bus does not record whether a signal has
        filled, so this policy has nothing to decide on -- and a study that
        recorded that as approval would be reporting a number it invented."""
        bus.write_signal_bus("Reversal Engine", "SELL", symbol="XAUUSD",
                             source_kind=bus.KIND_ENGINE)
        _note()
        got = {v["policy"]: v["action"] for v in study.verdicts(study.rows()[0]["id"])}
        assert got["freshest wins"] is None

    def test_half_size_records_the_multiplier(self, study):
        bus.write_signal_bus("Reversal Engine", "SELL", symbol="XAUUSD",
                             source_kind=bus.KIND_ENGINE)
        _note()
        got = {v["policy"]: v for v in study.verdicts(study.rows()[0]["id"])}
        assert got["half size"]["action"] == c.SHRINK
        assert got["half size"]["lot_mult"] == 0.5


class TestItNeverRaises:
    def test_a_broken_bus_read_does_not_reach_the_caller(self, study, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(clog, "_active_entries", _boom)
        _note()  # must not raise

    def test_a_broken_write_does_not_reach_the_caller(self, study, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("disk full")
        monkeypatch.setattr(clog._repo, "insert_observation", _boom)
        _note()  # must not raise

    def test_a_broken_settings_read_reads_as_off(self, study):
        class Hostile(dict):
            def get(self, *a, **k):
                raise RuntimeError("no")
        assert clog.enabled(Hostile()) is False


class TestTheReport:
    def test_it_counts_each_policys_verdicts_against_a_shared_denominator(self, study):
        bus.write_signal_bus("Reversal Engine", "SELL", symbol="XAUUSD",
                             source_kind=bus.KIND_ENGINE)
        _note(candidate_ref="1")
        _note(candidate_ref="2", direction="SELL")  # agrees, nothing opposing

        by_policy = {r["policy"]: r for r in study.report()}
        assert by_policy["first wins"]["observations"] == 2
        assert by_policy["first wins"]["block"] == 1
        assert by_policy["first wins"]["allow"] == 1
        assert by_policy["freshest wins"]["abstained"] == 1

    def test_an_empty_study_reports_nothing_rather_than_zeroes(self, study):
        assert study.report() == []
