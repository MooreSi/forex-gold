"""The second-message hold table.

A channel that posts a bare entry and then a separate TP/SL message needs the
two joined. A hold row buffers the bare signal until the follow-up arrives or
the window expires. Getting the selection wrong invents levels for a signal
that never got any, so the WHERE clauses are the point.
"""
from __future__ import annotations

import json

import pytest

from backend.src.db import database as db
from backend.src.services.positions import second_message_repo as repo


def _hold(conn, tg_id, channel="chan", seen=100.0, status="waiting", levels=None):
    conn.execute(
        "INSERT INTO vantage_second_message_holds "
        "(tg_message_id, channel_name, partial_json, first_seen_at, status, levels_json) "
        "VALUES (?,?,?,?,?,?)",
        (tg_id, channel, "{}", seen, status,
         json.dumps(levels) if levels is not None else None))


def _row(tg_id):
    with db.db() as conn:
        r = conn.execute("SELECT * FROM vantage_second_message_holds "
                         "WHERE tg_message_id=?", (tg_id,)).fetchone()
    return dict(r) if r else None


class TestGetWaitingHold:
    def test_finds_a_waiting_hold(self, fresh_db):
        with fresh_db.db() as conn:
            _hold(conn, "tg-1")
        assert repo.get_waiting_hold("tg-1")["tg_message_id"] == "tg-1"

    @pytest.mark.parametrize("status", ["resolved", "expired"])
    def test_a_hold_that_is_no_longer_waiting_is_not_returned(self, fresh_db, status):
        """The caller treats None as 'no hold exists yet' and inserts a fresh
        one. Returning a settled hold would replay a signal already handled."""
        with fresh_db.db() as conn:
            _hold(conn, "tg-1", status=status)
        assert repo.get_waiting_hold("tg-1") is None

    def test_an_unknown_id_is_none(self, fresh_db):
        assert repo.get_waiting_hold("nope") is None


class TestInsertHold:
    def test_creates_a_waiting_hold(self, fresh_db):
        repo.insert_hold("tg-1", "chan", "{}", 100.0)
        r = _row("tg-1")
        assert (r["status"], r["channel_name"], r["first_seen_at"]) == ("waiting", "chan", 100.0)

    def test_re_inserting_the_same_message_does_not_reset_it(self, fresh_db):
        """The buffered message is re-scanned every cycle. Without OR IGNORE
        the hold's first_seen_at would be pushed forward each time and the
        expiry window would never close."""
        repo.insert_hold("tg-1", "chan", "{}", 100.0)
        repo.insert_hold("tg-1", "chan", "{}", 999.0)
        assert _row("tg-1")["first_seen_at"] == 100.0


class TestAttachFollowupLevels:
    def test_attaches_to_the_waiting_hold_and_returns_its_id(self, fresh_db):
        with fresh_db.db() as conn:
            _hold(conn, "tg-1")
        assert repo.attach_followup_levels("chan", '{"tp1": 1}') == "tg-1"
        assert _row("tg-1")["levels_json"] == '{"tp1": 1}'

    def test_picks_the_newest_hold_only(self, fresh_db):
        """If a channel posted two bare entries back to back, a single
        follow-up belongs to the most recent one. Fanning it across both
        invents levels for a signal that never got any."""
        with fresh_db.db() as conn:
            _hold(conn, "old", seen=100.0)
            _hold(conn, "new", seen=200.0)
        assert repo.attach_followup_levels("chan", '{"tp1": 1}') == "new"
        assert _row("old")["levels_json"] is None

    def test_skips_a_hold_that_already_has_levels(self, fresh_db):
        with fresh_db.db() as conn:
            _hold(conn, "done", seen=200.0, levels={"tp1": 9})
            _hold(conn, "waiting", seen=100.0)
        assert repo.attach_followup_levels("chan", '{"tp1": 1}') == "waiting"

    def test_does_not_cross_channels(self, fresh_db):
        with fresh_db.db() as conn:
            _hold(conn, "other", channel="elsewhere", seen=200.0)
            _hold(conn, "mine", channel="chan", seen=100.0)
        assert repo.attach_followup_levels("chan", '{"tp1": 1}') == "mine"
        assert _row("other")["levels_json"] is None

    def test_a_channel_with_nothing_waiting_returns_none_and_writes_nothing(self, fresh_db):
        """The overwhelmingly common case -- most SL/TP-shaped chatter is not
        completing anything."""
        with fresh_db.db() as conn:
            _hold(conn, "tg-1", status="resolved")
        assert repo.attach_followup_levels("chan", '{"tp1": 1}') is None
        assert _row("tg-1")["levels_json"] is None


class TestMarkResolved:
    def test_marks_it_resolved(self, fresh_db):
        with fresh_db.db() as conn:
            _hold(conn, "tg-1")
        repo.mark_resolved("tg-1")
        assert _row("tg-1")["status"] == "resolved"

    def test_only_the_named_hold(self, fresh_db):
        with fresh_db.db() as conn:
            _hold(conn, "tg-1")
            _hold(conn, "tg-2")
        repo.mark_resolved("tg-1")
        assert _row("tg-2")["status"] == "waiting"
