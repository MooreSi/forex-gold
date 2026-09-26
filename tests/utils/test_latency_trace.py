"""The per-signal latency trace behind Settings > Latency (docs/todo/006).

Stamps are monotonic, so a gap between two of them is a real duration no
matter what the wall clock does. The one stamp that is not ours -- Telegram's
own post time -- arrives as a wall-clock time and is placed on the same
monotonic line by `mark_at`.
"""
from __future__ import annotations

import time

import pytest

from backend.src.utils import latency_trace as lt


@pytest.fixture(autouse=True)
def _clean():
    lt.clear()
    yield
    lt.clear()


def _stamp(key, stage, mono, monkeypatch):
    monkeypatch.setattr(lt.time, "monotonic", lambda: mono)
    lt.mark(key, stage)


class TestStamps:
    def test_a_gap_is_the_difference_between_two_stamps(self, monkeypatch):
        _stamp("1", "t1_arrived", 100.000, monkeypatch)
        _stamp("1", "t3_dequeued", 100.250, monkeypatch)

        assert lt.gap_ms("1", "t1_arrived", "t3_dequeued") == pytest.approx(250.0)

    def test_the_first_stamp_of_a_stage_wins(self, monkeypatch):
        """The scanner re-reads the whole buffer every pass, so t6_scanning
        is marked again on every rescan of a message it already handled.
        Overwriting moved the pick-up time to the LAST rescan -- after the
        order -- and made "decide" negative."""
        _stamp("1", "t6_scanning", 10.0, monkeypatch)
        _stamp("1", "t7_decided", 10.1, monkeypatch)
        _stamp("1", "t6_scanning", 99.0, monkeypatch)

        assert lt.gap_ms("1", "t6_scanning", "t7_decided") == pytest.approx(100.0)

    def test_a_missing_stage_is_no_gap_not_a_zero(self, monkeypatch):
        _stamp("1", "t1_arrived", 5.0, monkeypatch)

        assert lt.gap_ms("1", "t1_arrived", "t8_ordered") is None

    def test_an_empty_key_records_nothing(self):
        lt.mark(None, "t1_arrived")
        lt.mark("", "t1_arrived")

        assert lt.entries() == []


class TestWallClockStamps:
    def test_a_wall_time_lands_on_the_monotonic_line(self, monkeypatch):
        """Telegram's post time is a wall-clock time. Two seconds before now
        on the wall must be two seconds before now on the monotonic clock."""
        monkeypatch.setattr(lt.time, "time", lambda: 1_000.0)
        monkeypatch.setattr(lt.time, "monotonic", lambda: 50.0)
        lt.mark_at("7", "t0_posted", 998.0)
        lt.mark("7", "t1_arrived")

        assert lt.gap_ms("7", "t0_posted", "t1_arrived") == pytest.approx(2000.0)

    def test_the_wall_time_of_the_first_stamp_is_kept_for_display(self, monkeypatch):
        monkeypatch.setattr(lt.time, "time", lambda: 1_000.0)
        lt.mark("7", "t1_arrived")

        assert lt.entries()[0]["at"] == pytest.approx(1_000.0)


class TestPipelines:
    def test_an_untagged_trace_is_a_telegram_trace(self):
        """Every existing stamp is keyed by a Telegram message id and was
        written before pipelines existed."""
        lt.mark("42", "t1_arrived")

        assert [e["key"] for e in lt.entries("telegram")] == ["42"]
        assert lt.entries("engine") == []

    def test_a_tag_moves_a_trace_to_its_pipeline_and_names_it(self):
        lt.mark("bo:3", "e2_exec_start")
        lt.tag("bo:3", pipeline="engine", label="Breakout BO-0003")

        (e,) = lt.entries("engine")
        assert e["label"] == "Breakout BO-0003"
        assert lt.entries("telegram") == []

    def test_newest_first(self, monkeypatch):
        for i, t in enumerate((1.0, 3.0, 2.0)):
            monkeypatch.setattr(lt.time, "time", lambda t=t: t)
            lt.mark(str(i), "t1_arrived")

        assert [e["key"] for e in lt.entries()] == ["1", "2", "0"]


class TestTheRingIsBounded:
    def test_it_never_holds_more_than_the_cap(self):
        for i in range(lt._MAX_TRACES + 50):
            lt.mark(str(i), "t1_arrived")

        assert len(lt.entries()) <= lt._MAX_TRACES
        # the newest survive
        assert str(lt._MAX_TRACES + 49) in {e["key"] for e in lt.entries()}


def test_percentiles_of_nothing_is_empty():
    assert lt.percentiles([]) == {}


def test_percentiles_report_n_p50_p90_max():
    p = lt.percentiles([float(v) for v in range(1, 11)])

    assert p == {"n": 10, "p50": 6.0, "p90": 10.0, "p99": 10.0, "max": 10.0}


def test_real_clock_smoke():
    """No monkeypatching: a real pair of stamps gives a small positive gap."""
    lt.mark("r", "a")
    time.sleep(0.002)
    lt.mark("r", "b")

    assert 1.0 <= lt.gap_ms("r", "a", "b") < 1000.0
