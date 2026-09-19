"""The Parsing tab's message feed, and why it looked alive when nothing arrived.

Reported by the owner, 2026-09-19: "the feed also appears to be updating when
there are no new messages coming in".

It was. `fetch_stored_messages` selected seven columns and **not `id`**, so
every row reached the browser with no identity. `MessageFeedSection` keys its
list on `m.id ?? index`, which meant it keyed on the ARRAY POSITION.

React reconciles a list by key. With positional keys, one new message at the
top shifts every row down one slot, so React rewrites the text of every single
item in the feed rather than inserting one — the whole list visibly churns for
one arrival. It also means React cannot tell two polls of identical data apart
from a reordering, so there is no stable identity to diff against at all.

The fix is one column. These tests pin it, because a SELECT list is exactly
the kind of thing a later edit trims without knowing what depended on it.
"""
from __future__ import annotations

import sqlite3

import pytest

from backend.src.services.telegram import repo as tg_repo


@pytest.fixture
def feed_db(tmp_path, monkeypatch):
    """A telegram_messages table at the path the repo builds for itself.

    The repo opens its own connection by path rather than using the shared
    `db` module -- the read the Telegram page always made -- so pointing it
    somewhere safe means pointing its config at a temp directory.
    """
    db_path = tmp_path / "forex_trader_demo.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE telegram_messages ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " telegram_message_id TEXT, group_id TEXT, group_name TEXT,"
        " sender_name TEXT, timestamp TEXT, received_at TEXT, text TEXT,"
        " has_media INTEGER DEFAULT 0, media_type TEXT)"
    )
    conn.commit()
    conn.close()

    import backend.src.config as cfg
    monkeypatch.setattr(tg_repo, "_stored_messages_env", None, raising=False)
    monkeypatch.setattr(cfg, "DATA_DIR", tmp_path, raising=False)
    monkeypatch.setattr(cfg, "get", lambda key, default=None:
                        "demo" if key == "account_env" else default)
    return db_path


def _insert(db_path, text, group="GoldSignals"):
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO telegram_messages (telegram_message_id, group_id, "
        "group_name, sender_name, timestamp, received_at, text) "
        "VALUES (?,?,?,?,?,?,?)",
        ("t1", "g1", group, "sender", "2026-09-19T10:00:00Z",
         "2026-09-19T10:00:01Z", text),
    )
    conn.commit()
    conn.close()


class TestEveryRowCarriesItsIdentity:

    def test_a_row_has_an_id(self, feed_db):
        _insert(feed_db, "BUY 4000")

        rows, _ = tg_repo.fetch_stored_messages(10)

        assert rows[0]["id"] is not None

    def test_ids_are_distinct_so_the_browser_can_tell_rows_apart(self, feed_db):
        for i in range(3):
            _insert(feed_db, f"message {i}")

        rows, _ = tg_repo.fetch_stored_messages(10)

        assert len({r["id"] for r in rows}) == 3

    def test_an_arrival_does_not_change_any_existing_row_s_id(self, feed_db):
        # The whole point. If the identities are stable, React inserts one
        # row; if they move with position, it rewrites the entire feed.
        for i in range(3):
            _insert(feed_db, f"message {i}")
        before = [r["id"] for r in tg_repo.fetch_stored_messages(10)[0]]

        _insert(feed_db, "a new one")
        after = [r["id"] for r in tg_repo.fetch_stored_messages(10)[0]]

        assert after[1:] == before

    def test_two_reads_of_unchanged_data_are_identical(self, feed_db):
        # "Updating when nothing arrived" would also be true if the order
        # wandered between reads.
        for i in range(5):
            _insert(feed_db, f"message {i}")

        first, _ = tg_repo.fetch_stored_messages(10)
        second, _ = tg_repo.fetch_stored_messages(10)

        assert [r["id"] for r in first] == [r["id"] for r in second]


class TestWhatItStillReturns:

    def test_the_newest_message_is_first(self, feed_db):
        _insert(feed_db, "older")
        _insert(feed_db, "newer")

        rows, _ = tg_repo.fetch_stored_messages(10)

        assert rows[0]["text"] == "newer"

    def test_the_text_and_channel_still_come_back(self, feed_db):
        # Adding a column must not cost one.
        _insert(feed_db, "BUY 4000", group="GoldSignals")

        rows, _ = tg_repo.fetch_stored_messages(10)

        assert rows[0]["text"] == "BUY 4000"
        assert rows[0]["group_name"] == "GoldSignals"

    def test_the_total_counts_every_row_not_just_the_page(self, feed_db):
        for i in range(7):
            _insert(feed_db, f"message {i}")

        rows, total = tg_repo.fetch_stored_messages(3)

        assert (len(rows), total) == (3, 7)


class TestItDoesNotLeaveAHandleOpen:

    def test_the_connection_closes_even_when_the_query_fails(self, tmp_path,
                                                             monkeypatch):
        # Windows will not unlink a file that still has an open handle -- the
        # 50 teardown errors of 2026-08-27. A read that RAISED used to leak
        # one, because close() sat after the query rather than in a finally.
        #
        # Asserted by watching close(), not by unlinking the file: POSIX
        # happily unlinks an open file, so an unlink assertion would pass on
        # this machine no matter what the code did, and only fail on CI.
        db_path = tmp_path / "forex_trader_demo.db"
        sqlite3.connect(str(db_path)).close()   # exists, but has no table

        import backend.src.config as cfg
        monkeypatch.setattr(cfg, "DATA_DIR", tmp_path, raising=False)
        monkeypatch.setattr(cfg, "get", lambda key, default=None:
                            "demo" if key == "account_env" else default)

        closed = []

        class _Watched(sqlite3.Connection):
            # A subclass, because Connection.close is read-only and cannot be
            # patched on an instance.
            def close(self):
                closed.append(True)
                super().close()

        real_connect = sqlite3.connect
        monkeypatch.setattr(
            sqlite3, "connect",
            lambda *a, **k: real_connect(*a, **{**k, "factory": _Watched}),
        )

        with pytest.raises(sqlite3.Error):
            tg_repo.fetch_stored_messages(10)

        assert closed == [True]
