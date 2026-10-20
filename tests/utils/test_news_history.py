"""The kept history of economic-calendar events (docs/todo/023, #9).

The ForexFactory feed publishes the current week only, so a calendar that
marks past news days has to keep what it saw. These pin what is kept, for how
long, and that keeping it can never cost the feed cache or the blackout that
reads it.
"""
from __future__ import annotations

import json

from backend.src.utils import news_calendar as nc
from backend.src.utils import news_history as nh

DAY = 86400.0
NOW = 1_790_000_000.0


def ev(ts, title="CPI", currency="USD", impact="high"):
    return {"ts": ts, "title": title, "currency": currency, "impact": impact,
            "forecast": "", "previous": ""}


def test_merge_keeps_events_and_reads_them_back_in_a_range(tmp_path):
    path = tmp_path / "news_history.json"
    nh.merge(path, [ev(NOW), ev(NOW + DAY, "NFP")], now=NOW)

    got = nh.between(path, NOW - 1, NOW + 1)
    assert [e["title"] for e in got] == ["CPI"]


def test_merge_keeps_only_high_and_medium_impact(tmp_path):
    path = tmp_path / "news_history.json"
    nh.merge(path, [ev(NOW, "A", impact="high"), ev(NOW + 1, "B", impact="medium"),
                    ev(NOW + 2, "C", impact="low"), ev(NOW + 3, "D", impact="holiday")], now=NOW)

    assert [e["title"] for e in nh.between(path, 0, NOW + DAY)] == ["A", "B"]


def test_a_refetch_of_the_same_week_does_not_duplicate(tmp_path):
    path = tmp_path / "news_history.json"
    nh.merge(path, [ev(NOW)], now=NOW)
    nh.merge(path, [ev(NOW)], now=NOW)

    assert len(nh.between(path, 0, NOW + DAY)) == 1


def test_the_new_feed_is_the_truth_for_its_own_span(tmp_path):
    # Rescheduled: CPI moved an hour inside the week the feed covers. The old
    # time must go, or the calendar shows the event twice.
    path = tmp_path / "news_history.json"
    nh.merge(path, [ev(NOW - 10 * DAY, "Old"), ev(NOW), ev(NOW + DAY, "NFP")], now=NOW)
    nh.merge(path, [ev(NOW + 3600), ev(NOW + DAY, "NFP")], now=NOW)

    titles = [(e["title"], e["ts"]) for e in nh.between(path, 0, NOW + 2 * DAY)]
    assert titles == [("Old", NOW - 10 * DAY), ("CPI", NOW + 3600), ("NFP", NOW + DAY)]


def test_events_older_than_the_keep_window_are_pruned(tmp_path):
    path = tmp_path / "news_history.json"
    nh.merge(path, [ev(NOW - 401 * DAY, "Ancient"), ev(NOW - 399 * DAY, "Kept")], now=NOW)

    assert [e["title"] for e in nh.between(path, 0, NOW)] == ["Kept"]


def test_a_corrupt_file_reads_as_empty_and_is_rewritten(tmp_path):
    path = tmp_path / "news_history.json"
    path.write_text("{not json", encoding="utf-8")

    assert nh.between(path, 0, NOW) == []
    nh.merge(path, [ev(NOW)], now=NOW)
    assert json.loads(path.read_text(encoding="utf-8"))["events"][0]["title"] == "CPI"


def test_a_missing_file_reads_as_empty(tmp_path):
    assert nh.between(tmp_path / "nope.json", 0, NOW) == []


def test_saving_the_feed_cache_also_keeps_its_history():
    raw = [{"title": "CPI", "country": "USD", "date": "2026-09-15T12:30:00+00:00",
            "impact": "High", "forecast": "", "previous": ""}]
    nc._save_disk_cache(raw)

    got = nh.between(nc.history_file(), 0, 4_000_000_000)
    assert [(e["title"], e["currency"], e["impact"]) for e in got] == [("CPI", "USD", "high")]


def test_a_history_failure_never_costs_the_feed_cache(monkeypatch):
    # The cache is what the blackout falls back on after a restart. Keeping
    # history is a nicety; it must not be able to take that away.
    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(nh, "merge", boom)

    nc._save_disk_cache([{"title": "CPI", "country": "USD",
                          "date": "2026-09-15T12:30:00+00:00", "impact": "High"}])

    blob = json.loads(nc._disk_cache_file().read_text(encoding="utf-8"))
    assert blob["events"][0]["title"] == "CPI"
