"""The Dashboard's GEX card: the latest stored GLD snapshot, read-only.

What the card must never do is make a stale or missing snapshot look like
today's reading, or call a regime from nothing: a missing figure is None,
never 0, and an old snapshot says it is old.

Nothing here reaches the network, a broker or an order. The database is
private and temporary.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from backend.src.services.market import gex_repo
from backend.src.services.market import gex_report as report
from backend.src.services.reversal_engine import reversal_engine_repo as re_db
from tests.conftest import remove_db_file

DAY = 86400.0
NOW = 1_790_000_000.0


def snap(**kw):
    base = {"id": 1, "taken_at": NOW - 3600, "asof_date": "2026-09-29", "underlying": "GLD",
            "spot": 393.41, "xau_spot": 4250.0, "ratio": 10.8, "total_gex": -1.5e9,
            "flip_level": 393.91, "call_wall": 400.0, "put_wall": 380.0,
            "xau_flip_level": 4254.2, "xau_call_wall": 4320.0, "xau_put_wall": 4104.0,
            "n_rows": 1764, "expiries": '["2026-10-02", "2026-11-20"]',
            "source": "yfinance", "assumptions": "naive GEX: dealers long calls, short puts"}
    base.update(kw)
    return base


def test_no_snapshot_yet_says_so_and_reports_no_levels():
    out = report.summarise(None, n_snapshots=0, now=NOW)
    assert out["snapshot"] is None
    assert out["n_snapshots"] == 0
    assert out["regime"] is None
    assert out["stale"] is None


def test_the_levels_are_passed_through_in_both_units():
    out = report.summarise(snap(), n_snapshots=5, now=NOW)
    s = out["snapshot"]
    assert (s["flip_level"], s["xau_flip_level"]) == (393.91, 4254.2)
    assert (s["call_wall"], s["xau_call_wall"]) == (400.0, 4320.0)
    assert (s["put_wall"], s["xau_put_wall"]) == (380.0, 4104.0)
    assert s["expiries"] == ["2026-10-02", "2026-11-20"]
    assert out["n_snapshots"] == 5
    assert out["target_snapshots"] == report.TARGET_SNAPSHOTS


def test_the_regime_follows_the_sign_of_total_gex_and_is_never_guessed():
    assert report.summarise(snap(total_gex=-1.0), 1, NOW)["regime"] == "negative"
    assert report.summarise(snap(total_gex=2.0), 1, NOW)["regime"] == "positive"
    assert report.summarise(snap(total_gex=None), 1, NOW)["regime"] is None
    assert report.summarise(snap(total_gex=0.0), 1, NOW)["regime"] is None


def test_spot_is_placed_against_the_flip_only_when_there_is_one():
    assert report.summarise(snap(spot=390.0, flip_level=393.9), 1, NOW)["spot_vs_flip"] == "below"
    assert report.summarise(snap(spot=395.0, flip_level=393.9), 1, NOW)["spot_vs_flip"] == "above"
    assert report.summarise(snap(flip_level=None), 1, NOW)["spot_vs_flip"] is None


def test_a_weekend_gap_is_not_stale_but_a_missed_week_is():
    """Collected weekday evenings: Friday's snapshot is ~3 days old by Monday
    evening and still the latest there can be. Past 4 days, one was missed."""
    assert report.summarise(snap(taken_at=NOW - 3.2 * DAY), 1, NOW)["stale"] is False
    out = report.summarise(snap(taken_at=NOW - 6 * DAY), 1, NOW)
    assert out["stale"] is True
    assert out["age_days"] == pytest.approx(6.0)


@pytest.fixture
def re_tmp():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_db.init(path)
    yield
    re_db.close_db()
    remove_db_file(path)


def test_report_reads_the_latest_stored_snapshot_and_counts_the_history(re_tmp):
    assert report.report()["snapshot"] is None
    gex_repo.insert_snapshot(snap(asof_date="2026-09-28", total_gex=3.0), [])
    gex_repo.insert_snapshot(snap(asof_date="2026-09-29", total_gex=-3.0), [])
    out = report.report()
    assert out["snapshot"]["asof_date"] == "2026-09-29"
    assert out["regime"] == "negative"
    assert out["n_snapshots"] == 2
