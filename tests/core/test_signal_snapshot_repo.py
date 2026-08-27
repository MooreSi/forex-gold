"""The signal-snapshot fetch, moved into the positions repo.

Neither the age cutoff nor the ordering was pinned: dropping the cutoff and
reversing the sort both left the suite green. The cutoff is the reason a
snapshot means anything -- replaying a stale signal against today's market is
worse than having no reading, because the value of a snapshot is entirely that
it was taken at the time.
"""
from __future__ import annotations

from backend.src.services.positions import repo as positions_repo


def _sig(conn, tg_id, group="watched", parsed_at=1000.0):
    conn.execute(
        "INSERT INTO vantage_tg_signals (tg_message_id, group_id, group_name, "
        "raw_text, parsed_at) VALUES (?, 'g1', ?, 'raw', ?)",
        (tg_id, group, parsed_at))


def test_returns_signals_from_a_watched_group(fresh_db):
    with fresh_db.db() as conn:
        _sig(conn, "a", parsed_at=2000.0)
    got = positions_repo.fetch_recent_signals_for_groups(["watched"], 1000.0)
    assert [r["tg_message_id"] for r in got] == ["a"]


def test_ignores_groups_not_asked_for(fresh_db):
    with fresh_db.db() as conn:
        _sig(conn, "a", group="elsewhere", parsed_at=2000.0)
    assert positions_repo.fetch_recent_signals_for_groups(["watched"], 1000.0) == []


def test_excludes_anything_at_or_before_the_cutoff(fresh_db):
    """Strictly greater-than. A snapshot of a signal older than the window is
    a reading of the wrong market."""
    with fresh_db.db() as conn:
        _sig(conn, "old", parsed_at=999.0)
        _sig(conn, "exactly_at", parsed_at=1000.0)
        _sig(conn, "new", parsed_at=1001.0)
    got = positions_repo.fetch_recent_signals_for_groups(["watched"], 1000.0)
    assert [r["tg_message_id"] for r in got] == ["new"]


def test_returns_oldest_first(fresh_db):
    """The caller walks these in order and stops at a per-run cap, so the sort
    decides which signals get a snapshot at all when there are more than it
    can process."""
    with fresh_db.db() as conn:
        _sig(conn, "second", parsed_at=3000.0)
        _sig(conn, "first", parsed_at=2000.0)
        _sig(conn, "third", parsed_at=4000.0)
    got = positions_repo.fetch_recent_signals_for_groups(["watched"], 1000.0)
    assert [r["tg_message_id"] for r in got] == ["first", "second", "third"]


def test_spans_several_watched_groups(fresh_db):
    with fresh_db.db() as conn:
        _sig(conn, "a", group="one", parsed_at=2000.0)
        _sig(conn, "b", group="two", parsed_at=3000.0)
        _sig(conn, "c", group="three", parsed_at=4000.0)
    got = positions_repo.fetch_recent_signals_for_groups(["one", "two"], 1000.0)
    assert [r["tg_message_id"] for r in got] == ["a", "b"]
