"""Bucketing signal outcomes by distance to a scheduled event.

Research-only. The two things worth pinning are the ones a wrong sign or a
wrong denominator would hide: which side of the release a signal sits on, and
that a bucket reports how many DISTINCT EVENTS fed it, not just how many
signals. A cluster of 300 signals around one FOMC statement is one observation
about FOMC, not 300.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.src.services.backtest import event_buckets as eb


def _utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def _ts(dt):
    return dt.timestamp()


class TestTheScheduleRules:
    def test_nfp_is_the_first_friday_at_0830_new_york_in_summer(self):
        # 2026-08-07 is the first Friday of August; EDT is UTC-4.
        ev = [e for e in eb.scheduled_events(_utc(2026, 8, 1), _utc(2026, 8, 31))
              if e.title == "NFP"]
        assert [e.when for e in ev] == [_utc(2026, 8, 7, 12, 30)]

    def test_nfp_follows_the_clock_change_in_winter(self):
        # 2026-12-04 is a Friday in EST (UTC-5).
        ev = [e for e in eb.scheduled_events(_utc(2026, 12, 1), _utc(2026, 12, 31))
              if e.title == "NFP"]
        assert [e.when for e in ev] == [_utc(2026, 12, 4, 13, 30)]

    def test_fomc_statement_is_1400_new_york_on_a_listed_day(self):
        # (9, 17) is in news_calendar._FOMC_DATES for 2026.
        ev = [e for e in eb.scheduled_events(_utc(2026, 9, 1), _utc(2026, 9, 30))
              if e.title == "FOMC"]
        assert [e.when for e in ev] == [_utc(2026, 9, 17, 18, 0)]

    def test_events_outside_the_range_are_not_returned(self):
        ev = eb.scheduled_events(_utc(2026, 9, 18), _utc(2026, 9, 30))
        assert all(_utc(2026, 9, 18) <= e.when <= _utc(2026, 9, 30) for e in ev)
        assert not any(e.title == "FOMC" for e in ev)

    def test_every_scheduled_event_is_tier_one(self):
        assert {e.tier for e in eb.scheduled_events(
            _utc(2026, 1, 1), _utc(2026, 12, 31))} == {1}


class TestSignedDistance:
    def test_a_signal_after_the_release_is_positive(self):
        ev = [eb.Event(_utc(2026, 9, 17, 18, 0), 1, "FOMC")]
        d, e = eb.nearest(_ts(_utc(2026, 9, 17, 18, 20)), ev)
        assert d == pytest.approx(20.0) and e is ev[0]

    def test_a_signal_before_the_release_is_negative(self):
        ev = [eb.Event(_utc(2026, 9, 17, 18, 0), 1, "FOMC")]
        d, _ = eb.nearest(_ts(_utc(2026, 9, 17, 17, 40)), ev)
        assert d == pytest.approx(-20.0)

    def test_the_nearer_of_two_events_is_chosen(self):
        a = eb.Event(_utc(2026, 9, 17, 12, 0), 1, "A")
        b = eb.Event(_utc(2026, 9, 17, 18, 0), 1, "B")
        d, e = eb.nearest(_ts(_utc(2026, 9, 17, 17, 0)), [a, b])
        assert e is b and d == pytest.approx(-60.0)

    def test_no_events_gives_none(self):
        assert eb.nearest(0.0, []) == (None, None)


class TestBucketLabels:
    @pytest.mark.parametrize("minutes,label", [
        (-200, "none"), (-90, "-120..-60"), (-45, "-60..-30"), (-5, "-15..0"),
        (0, "0..15"), (14.9, "0..15"), (15, "15..30"), (90, "60..120"),
        (121, "none"), (None, "none"),
    ])
    def test_edges(self, minutes, label):
        assert eb.bucket_of(minutes) == label


class TestSummary:
    def test_a_bucket_counts_distinct_events_not_just_signals(self):
        fomc = eb.Event(_utc(2026, 9, 17, 18, 0), 1, "FOMC")
        rows = [{"ts": _ts(_utc(2026, 9, 17, 18, 5 + i)), "r": -1.0}
                for i in range(8)]
        out = {b.label: b for b in eb.summarise(rows, [fomc])}
        assert out["0..15"].n == 8 and out["0..15"].events == 1

    def test_the_baseline_is_the_far_from_any_event_bucket(self):
        fomc = eb.Event(_utc(2026, 9, 17, 18, 0), 1, "FOMC")
        rows = ([{"ts": _ts(_utc(2026, 9, 16, 10, i)), "r": 0.5} for i in range(10)]
                + [{"ts": _ts(_utc(2026, 9, 17, 18, 5)), "r": -1.5}])
        out = {b.label: b for b in eb.summarise(rows, [fomc])}
        assert out["none"].mean_r == pytest.approx(0.5)
        assert out["0..15"].mean_r == pytest.approx(-1.5)
        assert out["0..15"].diff_vs_none == pytest.approx(-2.0)

    def test_a_thin_bucket_is_flagged_not_trusted(self):
        fomc = eb.Event(_utc(2026, 9, 17, 18, 0), 1, "FOMC")
        rows = [{"ts": _ts(_utc(2026, 9, 17, 18, 5)), "r": -1.0}]
        out = {b.label: b for b in eb.summarise(rows, [fomc])}
        assert out["0..15"].thin is True

    def test_rows_without_a_label_are_ignored(self):
        fomc = eb.Event(_utc(2026, 9, 17, 18, 0), 1, "FOMC")
        rows = [{"ts": _ts(_utc(2026, 9, 17, 18, 5)), "r": None}]
        assert eb.summarise(rows, [fomc]) == []


class TestCsvEvents:
    def test_events_are_read_from_csv(self, tmp_path):
        p = tmp_path / "e.csv"
        p.write_text("utc,tier,title\n2026-08-12T12:30:00+00:00,1,CPI\n")
        ev = eb.load_events_csv(p)
        assert ev == [eb.Event(_utc(2026, 8, 12, 12, 30), 1, "CPI")]

    def test_a_naive_timestamp_is_refused_rather_than_guessed(self, tmp_path):
        p = tmp_path / "e.csv"
        p.write_text("utc,tier,title\n2026-08-12T12:30:00,1,CPI\n")
        with pytest.raises(ValueError):
            eb.load_events_csv(p)
