"""How long the Bounce engine has been producing nothing, and what stopped it.

`docs/simon-handover/034`: the engine's last signal was 2026-08-27 05:15 UTC,
and in the sixteen days after it it found 424 entry candidates and refused
every one. Nothing on screen said so. A signal generator that has produced
nothing for a fortnight looks exactly like one in a quiet market, and the panel
said "running" throughout.

`get_silence_report` is the read behind that. It answers from data rather than
from a hardcoded list of refusal names: a cycle counts as a candidate when the
analysis row carries one, so a gate added later is counted without anyone
remembering to add it here.
"""
from __future__ import annotations

from backend.src.services.test_signal import silence_repo as silence_db
from backend.src.services.test_signal import test_signal_repo as db


def _signal(at: float) -> None:
    db.insert_signal({
        "created_at": at, "direction": "BUY", "entry_low": 1.0,
        "entry_high": 2.0, "entry_mid": 1.5, "stop_loss": 0.5,
    })


def _cycle(at: float, result: str, candidate: bool) -> None:
    db.log_analysis({
        "ts": at, "result": result,
        "candidate": {"direction": "BUY", "trigger_pattern": "bounce"} if candidate else None,
    })


class TestWhenTheEngineIsProducing:
    def test_a_recent_signal_leaves_nothing_to_report(self, fresh_db):
        _signal(1000.0)

        report = silence_db.get_silence_report(now=1100.0)

        assert report["last_signal_at"] == 1000.0
        assert report["silent_secs"] == 100.0

    def test_only_cycles_after_the_last_signal_are_counted(self, fresh_db):
        """The window is "since it last managed to produce one". Refusals from
        before that are the engine working, not the engine stuck."""
        _cycle(500.0, "ml_gate", candidate=True)
        _signal(1000.0)
        _cycle(1500.0, "ml_gate", candidate=True)

        report = silence_db.get_silence_report(now=2000.0)

        assert report["candidates"] == 1
        assert report["refusals"] == [("ml_gate", 1)]


    def test_the_window_starts_at_the_latest_signal_not_the_first(self, fresh_db):
        """Two signals, and the count runs from the second. Taking the earliest
        would make a busy engine look permanently stuck, and would grow the
        number for as long as the engine kept working."""
        _signal(1000.0)
        _cycle(1100.0, "ml_gate", candidate=True)
        _signal(2000.0)
        _cycle(2100.0, "ml_gate", candidate=True)

        report = silence_db.get_silence_report(now=3000.0)

        assert report["last_signal_at"] == 2000.0
        assert report["candidates"] == 1

    def test_a_cycle_at_the_very_moment_of_the_signal_is_not_counted(self, fresh_db):
        """The cycle that produced the signal shares its timestamp. Counting it
        would report one refused candidate on an engine that had just succeeded
        -- off by one, in the direction that invents a problem."""
        _signal(1000.0)
        _cycle(1000.0, "ml_gate", candidate=True)

        report = silence_db.get_silence_report(now=2000.0)

        assert report["candidates"] == 0
        assert report["refusals"] == []


class TestWhatStoppedIt:
    def test_refusals_come_back_commonest_first(self, fresh_db):
        _signal(1000.0)
        for _ in range(3):
            _cycle(1100.0, "ml_gate", candidate=True)
        _cycle(1200.0, "quality_low", candidate=True)

        report = silence_db.get_silence_report(now=2000.0)

        assert report["refusals"] == [("ml_gate", 3), ("quality_low", 1)]
        assert report["candidates"] == 4

    def test_a_cycle_that_never_found_a_candidate_is_not_a_refusal(self, fresh_db):
        """`no_trigger` is the commonest row in the log by an order of
        magnitude and says nothing about why the engine is silent -- it found
        nothing to judge. Counting it would bury the gate that is actually
        binding."""
        _signal(1000.0)
        for _ in range(50):
            _cycle(1100.0, "no_trigger", candidate=False)
        _cycle(1200.0, "ml_gate", candidate=True)

        report = silence_db.get_silence_report(now=2000.0)

        assert report["candidates"] == 1
        assert report["refusals"] == [("ml_gate", 1)]

    def test_a_gate_this_function_has_never_heard_of_is_still_counted(self, fresh_db):
        """No hardcoded taxonomy: whatever `result` the engine writes is what
        comes back. A gate added next year needs no change here."""
        _signal(1000.0)
        _cycle(1100.0, "some_future_gate", candidate=True)

        report = silence_db.get_silence_report(now=2000.0)

        assert report["refusals"] == [("some_future_gate", 1)]


class TestTheEmptyCases:
    def test_an_engine_that_has_never_produced_a_signal_counts_everything(self, fresh_db):
        _cycle(100.0, "ml_gate", candidate=True)
        _cycle(200.0, "quality_low", candidate=True)

        report = silence_db.get_silence_report(now=1000.0)

        assert report["last_signal_at"] is None
        assert report["silent_secs"] is None
        assert report["candidates"] == 2

    def test_a_brand_new_database_reports_nothing_rather_than_raising(self, fresh_db):
        report = silence_db.get_silence_report(now=1000.0)

        assert report == {
            "last_signal_at": None, "silent_secs": None,
            "candidates": 0, "refusals": [],
        }
