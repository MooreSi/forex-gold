"""What the channel scorecard counts, and the one thing it counts twice.

`get_channel_scorecard` is not a report. `recompute_channel_performance` turns
it into `channel_performance.lot_mult` and `paused`, and `resolution.py` reads
that row before every trade: a paused channel is refused and an unpaused one
has its lot scaled. So the arithmetic here decides position size, and until now
one test exercised it (`tests/core/test_core_db_channel_scorecard.py`, a
NameError regression).

It merges this node's `vantage_simulated_trades` with the `consolidated_trades`
ledger, because a paired node's trades close in that node's own database and
reach this one only through the ledger. The merge is what these tests are
about: what it must count, what it must not, and the case where it gets it
wrong today.

**`TestTheSameTradeUnderTwoIds` records a DEFECT** — `docs/todo/bugs/054`. It is
pinned rather than fixed because the fix changes a sizing input, and pinned at
all so that fixing it is a deliberate, visible change with a safety net under
the cases that are already right.
"""
from __future__ import annotations

import time

import pytest

from backend.src.db import database as db
from backend.src.services.channels import repo as ch_repo


def _local_trade(conn, trade_id, *, source, ticket, pnl, close_time):
    conn.execute(
        "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
        "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
        (f"sig-{trade_id}", "BUY", 3999.0, 4001.0, 3995.0, "filled", close_time - 60),
    )
    conn.execute(
        "INSERT INTO vantage_simulated_trades "
        "(trade_id, signal_id, mt5_ticket, direction, entry_low, entry_high, entry_price, "
        " lot_size, remaining_lots, stop_loss, status, open_time, close_time, close_price, "
        " net_pnl, tg_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, f"sig-{trade_id}", ticket, "BUY", 3999.0, 4001.0, 4000.0,
         0.1, 0.0, 3995.0, "closed", close_time - 60, close_time, 4010.0,
         pnl, source),
    )


def _ledger_trade(conn, trade_id, *, source, ticket, pnl, close_time, engine="reversal_engine"):
    conn.execute(
        "INSERT INTO consolidated_trades "
        "(node_id, trade_id, engine, direction, open_time, close_time, pnl_dollars, "
        " outcome, received_at, tg_source, mt5_ticket) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("node-a", trade_id, engine, "BUY", close_time - 60, close_time, pnl,
         "win" if pnl > 0 else "loss", close_time, source, ticket),
    )


def _scorecard_for(source: str):
    for row in ch_repo.get_channel_scorecard(days=30):
        if row["source"] == source:
            return row
    return None


@pytest.fixture
def ledger_ready(fresh_db):
    """The ledger table is created lazily by the sync layer."""
    ch_repo._ensure_sync_tables()
    return fresh_db


class TestTheMergeDoesItsJob:
    def test_a_trade_only_this_node_has_is_counted_once(self, ledger_ready):
        now = time.time()
        with db.db() as conn:
            _local_trade(conn, "t1", source="Chan A", ticket=111, pnl=10.0, close_time=now)

        row = _scorecard_for("Chan A")

        assert row["trades"] == 1
        assert row["net_pnl"] == pytest.approx(10.0)

    def test_a_trade_only_the_ledger_has_is_counted(self, ledger_ready):
        """The reason the merge exists: a paired node's trade closes in that
        node's database and never gets a local row."""
        now = time.time()
        with db.db() as conn:
            _ledger_trade(conn, "remote-1", source="Chan A", ticket=222, pnl=25.0, close_time=now)

        row = _scorecard_for("Chan A")

        assert row["trades"] == 1
        assert row["net_pnl"] == pytest.approx(25.0)

    def test_the_same_trade_id_in_both_places_is_counted_once(self, ledger_ready):
        now = time.time()
        with db.db() as conn:
            _local_trade(conn, "t1", source="Chan A", ticket=111, pnl=10.0, close_time=now)
            _ledger_trade(conn, "t1", source="Chan A", ticket=111, pnl=10.0, close_time=now)

        row = _scorecard_for("Chan A")

        assert row["trades"] == 1
        assert row["net_pnl"] == pytest.approx(10.0)

    def test_a_ledger_row_with_no_ticket_is_not_a_real_trade(self, ledger_ready):
        """Virtual engine closes reach the ledger too. Only rows carrying a
        broker ticket are trades that happened."""
        now = time.time()
        with db.db() as conn:
            _ledger_trade(conn, "virtual-1", source="Chan A", ticket=None,
                          pnl=99.0, close_time=now)

        assert _scorecard_for("Chan A") is None


class TestItDoesNotCrashWithNoLocalTradesAtAll:
    """`docs/todo/bugs/056`, and the second time this function has done it.

    `_session_for_hour` and `_trade_pts` were bound INSIDE the loop over local
    rows and then used in the loop over ledger rows. With no local trade in the
    window and any ledger row carrying a ticket, the second loop reached an
    unbound name and the whole call raised `UnboundLocalError`.

    The first version of this bug — the same two helpers, missing from this
    module entirely — is what `tests/core/test_core_db_channel_scorecard.py`
    exists for, and it *"crashed the app for real on a demo->live account
    switch"*. A fresh account with a ledger pulled from a peer is exactly that
    situation again.
    """

    def test_a_ledger_only_scorecard_does_not_raise(self, ledger_ready):
        now = time.time()
        with db.db() as conn:
            _ledger_trade(conn, "remote-1", source="Chan A", ticket=222,
                          pnl=25.0, close_time=now)

        rows = ch_repo.get_channel_scorecard(days=30)

        assert [r["source"] for r in rows] == ["Chan A"]

    def test_the_ledger_row_still_lands_in_its_session_bucket(self, ledger_ready):
        """The session split is what the unbound helper was for, so the fix has
        to keep computing it, not just stop raising."""
        now = time.time()
        with db.db() as conn:
            _ledger_trade(conn, "remote-1", source="Chan A", ticket=222,
                          pnl=25.0, close_time=now)

        row = _scorecard_for("Chan A")

        assert sum(row["sessions"].values()) == pytest.approx(25.0)


class TestTheSameTradeUnderTwoIds:
    """**`docs/todo/bugs/054`, fixed 2026-09-13 on the owner's instruction.**

    The dedup was `if tid in local_ids: continue`, on `trade_id` alone. The main
    close path pushes a trade to the ledger under the vantage trade_id; the
    Reversal Engine pushes the same broker trade under its own signal ref. The
    ids never match, so the ticket was counted twice.

    Live on 2026-09-12: 45 of the Reversal Engine's 46 ticket-carrying ledger
    rows were duplicates of an existing local row, worth +$229.26 — the winners
    — which reported that channel at 54.5% and -$1,833 instead of 51.3% and
    -$2,067. The 55.0% line multiplies its lot size by 1.3.

    These two cases were written on 2026-09-12 asserting the broken answers,
    deliberately, so the fix would be a visible change rather than a silent
    one, and each carried a message saying what to change. This is that change.
    """

    def test_one_broker_ticket_under_two_ids_is_counted_once(self, ledger_ready):
        now = time.time()
        with db.db() as conn:
            _local_trade(conn, "72f81cd0", source="Chan A", ticket=999, pnl=30.0, close_time=now)
            _ledger_trade(conn, "RE-36B872", source="Chan A", ticket=999, pnl=30.0, close_time=now)

        row = _scorecard_for("Chan A")

        assert row["trades"] == 1
        assert row["net_pnl"] == pytest.approx(30.0)

    def test_the_local_row_is_the_one_that_survives(self, ledger_ready):
        """Not just "counted once" — counted from the LOCAL row, which carries
        entry and close prices. A ledger row has none, so its points-based
        contribution is zero and taking it instead would quietly flatten
        avg_pts and payoff_rr for the whole channel."""
        now = time.time()
        with db.db() as conn:
            _local_trade(conn, "72f81cd0", source="Chan A", ticket=999, pnl=30.0, close_time=now)
            _ledger_trade(conn, "RE-36B872", source="Chan A", ticket=999, pnl=99.0, close_time=now)

        assert _scorecard_for("Chan A")["net_pnl"] == pytest.approx(30.0)

    def test_a_zero_ticket_ledger_row_is_not_a_real_trade(self, ledger_ready):
        """`mt5_ticket = 0` is this app's placeholder for "not placed at a
        broker" (bugs/016). The ledger query filtered `IS NOT NULL`, and 0 is
        not NULL, so a trade that never reached a broker was scored as one."""
        now = time.time()
        with db.db() as conn:
            _ledger_trade(conn, "RE-4BFC73", source="Chan A", ticket=0,
                          pnl=4.15, close_time=now)

        assert _scorecard_for("Chan A") is None

    def test_the_id_check_still_earns_its_place(self, ledger_ready):
        """Both halves are needed, and this is the case only the id catches.

        A local row can carry `mt5_ticket = 0` — the placeholder for a trade
        the broker has not confirmed yet (bugs/016) — and a zero is not put in
        the ticket set. If the ledger then carries the same trade under its own
        id with a real ticket, only the id match stops it being counted twice.
        Delete `tid in local_ids` and this is what notices."""
        now = time.time()
        with db.db() as conn:
            _local_trade(conn, "t1", source="Chan A", ticket=0, pnl=12.0, close_time=now)
            _ledger_trade(conn, "t1", source="Chan A", ticket=777, pnl=12.0, close_time=now)

        row = _scorecard_for("Chan A")

        assert row["trades"] == 1
        assert row["net_pnl"] == pytest.approx(12.0)

    def test_a_remote_trade_with_its_own_ticket_still_counts(self, ledger_ready):
        """The merge's whole purpose, re-asserted beside the fix: deduping on
        ticket must not start discarding the paired node's trades, which are
        exactly the rows with no local twin."""
        now = time.time()
        with db.db() as conn:
            _local_trade(conn, "local-1", source="Chan A", ticket=111, pnl=10.0, close_time=now)
            _ledger_trade(conn, "RE-OTHER", source="Chan A", ticket=222, pnl=25.0, close_time=now)

        row = _scorecard_for("Chan A")

        assert row["trades"] == 2
        assert row["net_pnl"] == pytest.approx(35.0)


class TestTheWindow:
    def test_a_trade_older_than_the_window_is_not_counted(self, ledger_ready):
        now = time.time()
        with db.db() as conn:
            _local_trade(conn, "old", source="Chan A", ticket=111,
                         pnl=10.0, close_time=now - 40 * 86400)

        assert _scorecard_for("Chan A") is None
