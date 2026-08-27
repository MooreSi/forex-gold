"""The REF signal backfill's two statements.

All three arms of "do not record the same message twice" were untested:
dropping the LEFT JOIN ... IS NULL, turning OR IGNORE into OR REPLACE, and
counting attempts instead of writes each left the suite green.

They matter together. This table feeds the lead/lag correlation between what a
Telegram channel posted and what the Reversal Engine did, so a message counted
twice does not just inflate a number, it changes which system looks like it
fired first.
"""
from __future__ import annotations

from backend.src.services.positions import repo as positions_repo


def _msg(conn, msg_id, received_at="2026-08-20T00:00:00+00:00", text="BUY 4000",
         group_id="g1", group_name="chan"):
    conn.execute(
        "INSERT INTO telegram_messages (telegram_message_id, group_id, group_name, "
        "sender_name, timestamp, received_at, text) VALUES (?,?,?,'s','ts',?,?)",
        (msg_id, group_id, group_name, received_at, text))


def _record(msg_id, group_id="g1", parsed_at=1000.0):
    return (str(msg_id), group_id, "chan", "s", "ts", "BUY 4000", parsed_at,
            "BUY", 3999.0, 4001.0, 3990.0, *([None] * 8), "backfilled")


def _signal_rows():
    from backend.src.db import database as db
    with db.db() as conn:
        return [r[0] for r in conn.execute(
            "SELECT tg_message_id FROM vantage_tg_signals").fetchall()]


class TestFetchUnparsedTelegramMessages:
    def test_returns_a_message_with_no_signal_row(self, fresh_db):
        with fresh_db.db() as conn:
            _msg(conn, "m1")
        got = positions_repo.fetch_unparsed_telegram_messages("2026-08-01T00:00:00+00:00", 10)
        assert [r["telegram_message_id"] for r in got] == ["m1"]

    def test_skips_a_message_that_already_became_a_signal(self, fresh_db):
        """The LEFT JOIN ... IS NULL is what makes this a backfill rather than
        a re-parse. Re-recording a handled message double-counts it in every
        correlation built on this table."""
        with fresh_db.db() as conn:
            _msg(conn, "m1")
        positions_repo.insert_backfilled_signals([_record("m1")])
        assert positions_repo.fetch_unparsed_telegram_messages(
            "2026-08-01T00:00:00+00:00", 10) == []

    def test_the_join_matches_on_group_as_well_as_message_id(self, fresh_db):
        """Message ids are only unique within a group. A signal recorded for
        the same id in a different group must not mask this one."""
        with fresh_db.db() as conn:
            _msg(conn, "m1", group_id="g2")
        positions_repo.insert_backfilled_signals([_record("m1", group_id="g1")])
        got = positions_repo.fetch_unparsed_telegram_messages(
            "2026-08-01T00:00:00+00:00", 10)
        assert [r["telegram_message_id"] for r in got] == ["m1"]

    def test_excludes_messages_older_than_the_cutoff(self, fresh_db):
        with fresh_db.db() as conn:
            _msg(conn, "old", received_at="2026-07-01T00:00:00+00:00")
            _msg(conn, "new", received_at="2026-08-20T00:00:00+00:00")
        got = positions_repo.fetch_unparsed_telegram_messages(
            "2026-08-01T00:00:00+00:00", 10)
        assert [r["telegram_message_id"] for r in got] == ["new"]

    def test_excludes_messages_with_no_text(self, fresh_db):
        with fresh_db.db() as conn:
            _msg(conn, "m1", text=None)
        assert positions_repo.fetch_unparsed_telegram_messages(
            "2026-08-01T00:00:00+00:00", 10) == []

    def test_honours_the_limit_taking_the_oldest(self, fresh_db):
        with fresh_db.db() as conn:
            _msg(conn, "a", received_at="2026-08-10T00:00:00+00:00")
            _msg(conn, "b", received_at="2026-08-11T00:00:00+00:00")
            _msg(conn, "c", received_at="2026-08-12T00:00:00+00:00")
        got = positions_repo.fetch_unparsed_telegram_messages(
            "2026-08-01T00:00:00+00:00", 2)
        assert [r["telegram_message_id"] for r in got] == ["a", "b"]


class TestInsertBackfilledSignals:
    def test_records_new_rows_and_returns_the_count(self, fresh_db):
        assert positions_repo.insert_backfilled_signals(
            [_record("m1"), _record("m2")]) == 2
        assert sorted(_signal_rows()) == ["m1", "m2"]

    def test_an_already_recorded_message_is_neither_rewritten_nor_counted(self, fresh_db):
        """Both halves of the same property. OR REPLACE would overwrite a row
        the live parser may since have updated, and counting the attempt would
        report a backfill that did not happen."""
        positions_repo.insert_backfilled_signals([_record("m1", parsed_at=1000.0)])
        assert positions_repo.insert_backfilled_signals(
            [_record("m1", parsed_at=9999.0)]) == 0

        from backend.src.db import database as db
        with db.db() as conn:
            kept = conn.execute("SELECT parsed_at FROM vantage_tg_signals "
                                "WHERE tg_message_id='m1'").fetchone()[0]
        assert kept == 1000.0, "the existing row was overwritten"

    def test_a_mixed_batch_counts_only_the_new_ones(self, fresh_db):
        positions_repo.insert_backfilled_signals([_record("m1")])
        assert positions_repo.insert_backfilled_signals(
            [_record("m1"), _record("m2"), _record("m3")]) == 2

    def test_an_empty_batch_is_a_no_op(self, fresh_db):
        assert positions_repo.insert_backfilled_signals([]) == 0
        assert _signal_rows() == []
