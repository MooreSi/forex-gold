"""A profit-sync correction must reach the consolidated ledger, not just the row.

bugs/025, live: a `trade_closed` with no `close_price` made `record_close`
compute `(0 - 4478.35) x 0.1 x 100` = **-$44,783.50**, write it to the trade
row, and push it to the consolidated ledger. The bridge-side guard that stops
that computation shipped on 2026-09-04 and is tested separately
(`test_a_priceless_close_is_not_booked_at_zero.py`).

**What was still missing is the other half: the repair.** `sync_profit` later
asks the broker what the trade really made (-$635.80), corrects `net_pnl` and
the simulated balance -- and stops there. `consolidated_trades.pnl_dollars`
keeps the estimate forever. On 2026-09-11 three such rows were still carrying
-$44,821.00 / -$44,783.50 / -$44,746.50 against local rows reading -$264.00 /
-$635.80 / -$609.10: **-$132,842 of fiction in the table every cross-node P&L,
win rate and Edge Dashboard figure is read from.**

This is not specific to that incident. EVERY correction is dropped; the other
ones are pennies, which is why nobody saw it.

The ledger is AMENDED, never created here: a row this node never pushed at
close has no engine, direction or strategy to invent, and a half-row in the
cross-node ledger is worse than an absent one.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.src.db import database as db
from backend.src.services.broker.ea_bridge import comment_for_trade
from backend.src.services.trading import profit_sync as core_profit_sync
from tests._fakes import _ReconciliationBridge

TRADE  = "edba0ff6-f2b9-46"
TICKET = 1935433548
ENTRY  = 4478.35
ESTIMATE = -44783.50      # what the priceless close computed
REAL     = -635.80        # what the broker actually settled


def _seed_trade(net_pnl: float) -> None:
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{TRADE}", "BUY", ENTRY, ENTRY, 4473.35, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, close_time, close_price, net_pnl, mt5_profit, strategy, tg_source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (TRADE, f"sig-{TRADE}", TICKET, "BUY", ENTRY, ENTRY, ENTRY, 0.1, 0.0, 4473.35,
             "closed", time.time() - 600, time.time(), 0.0, net_pnl, None,
             "template:30 TP1 SL50 and Trail", "Reversal Engine"),
        )


def _seed_ledger_row(pnl: float, outcome: str) -> str:
    """The row record_close pushed at close time, with the estimate in it."""
    node_id = db.get_or_create_node_id()
    db.record_consolidated_trade(node_id, {
        "trade_id":    TRADE,
        "engine":      "main",
        "direction":   "BUY",
        "strategy":    "template:30 TP1 SL50 and Trail",
        "open_time":   time.time() - 600,
        "close_time":  time.time(),
        "pnl_dollars": pnl,
        "outcome":     outcome,
        "tg_source":   "Reversal Engine",
        "mt5_ticket":  TICKET,
        "max_tp_hit":  "none",
        "rr":          0.796407185628664,
    })
    return node_id


def _ledger_row(trade_id: str = TRADE) -> dict | None:
    db._ensure_sync_tables()
    with db.db() as conn:
        row = conn.execute(
            "SELECT * FROM consolidated_trades WHERE trade_id=?", (trade_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def _bridge_settling(profit: float) -> _ReconciliationBridge:
    """The broker with this trade's single anchor leg closed at `profit`.

    `_ReconciliationBridge` from tests/_fakes rather than a 51st local
    `_FakeBridge` -- tests/refactor/test_fixture_dedup.py counts those and they
    are shrink-only.
    """
    comment = comment_for_trade(TRADE)
    deals = [
        {"entry": 0, "comment": f"{comment}a1", "price": ENTRY, "volume": 0.1},
        {"entry": 1, "profit": profit, "swap": 0.0, "fee": 0.0},
    ]
    return _ReconciliationBridge(
        positions=[],
        position_history=deals,
        deal_history=[{**d, "position_id": TICKET} for d in deals],
    )


class TestWhenTheBrokerCorrectsTheFigure:
    def test_the_ledger_carries_the_corrected_pnl(self, fresh_db):
        """The bug itself: -$44,783.50 must not survive in the ledger once the
        broker has said the trade lost $635.80."""
        _seed_trade(net_pnl=ESTIMATE)
        _seed_ledger_row(pnl=ESTIMATE, outcome="loss")

        asyncio.run(core_profit_sync.sync_profit(TRADE, TICKET, _bridge_settling(REAL)))

        assert _ledger_row()["pnl_dollars"] == pytest.approx(REAL)

    def test_the_outcome_is_regraded_against_the_corrected_pnl(self, fresh_db):
        """A correction can cross zero. An estimate graded `loss` that the
        broker settles as a profit must not stay a loss -- that column is what
        every win rate on the Edge Dashboard counts."""
        _seed_trade(net_pnl=ESTIMATE)
        _seed_ledger_row(pnl=ESTIMATE, outcome="loss")

        asyncio.run(core_profit_sync.sync_profit(TRADE, TICKET, _bridge_settling(41.20)))

        assert _ledger_row()["outcome"] == "win"

    def test_the_later_follow_up_columns_are_not_wiped(self, fresh_db):
        """`max_tp_hit` and `rr` are filled by a separate push 30+ min after
        the close. An amend that dropped them would undo that."""
        _seed_trade(net_pnl=ESTIMATE)
        _seed_ledger_row(pnl=ESTIMATE, outcome="loss")

        asyncio.run(core_profit_sync.sync_profit(TRADE, TICKET, _bridge_settling(REAL)))

        row = _ledger_row()
        assert row["max_tp_hit"] == "none"
        assert row["rr"] == pytest.approx(0.796407185628664)


class TestWhatItMustNotDo:
    def test_no_ledger_row_is_invented_for_a_trade_that_has_none(self, fresh_db):
        """Amend only. A row with no engine/direction/strategy would be worse
        in the cross-node ledger than no row at all."""
        _seed_trade(net_pnl=ESTIMATE)

        asyncio.run(core_profit_sync.sync_profit(TRADE, TICKET, _bridge_settling(REAL)))

        assert _ledger_row() is None

    def test_an_uncorrected_trade_leaves_the_row_exactly_as_it_was(self, fresh_db):
        """The broker agreeing with the estimate must not rewrite the row --
        a needless upsert is a needless chance to clobber it."""
        _seed_trade(net_pnl=REAL)
        _seed_ledger_row(pnl=REAL, outcome="loss")
        before = _ledger_row()

        asyncio.run(core_profit_sync.sync_profit(TRADE, TICKET, _bridge_settling(REAL)))

        assert _ledger_row() == before

    def test_a_peers_row_is_not_adopted_as_our_own(self, fresh_db):
        """consolidated_trades is keyed (node_id, trade_id) and holds every
        paired node's rows. A trade the PEER closed and we merely synced has
        no row of ours -- reading the peer's and writing it back under our
        node_id would fabricate a second row for a trade this node never
        closed, and double it in every total. (Dropping the node_id filter on
        the lookup survives every other test in this file.)"""
        _seed_trade(net_pnl=ESTIMATE)
        db.record_consolidated_trade("peernode0001", {
            "trade_id": TRADE, "engine": "main", "direction": "BUY",
            "strategy": "template:30 TP1 SL50 and Trail", "open_time": time.time() - 600,
            "close_time": time.time(), "pnl_dollars": ESTIMATE, "outcome": "loss",
            "tg_source": "Reversal Engine", "mt5_ticket": TICKET,
        })

        asyncio.run(core_profit_sync.sync_profit(TRADE, TICKET, _bridge_settling(REAL)))

        db._ensure_sync_tables()
        with db.db() as conn:
            rows = conn.execute(
                "SELECT node_id, pnl_dollars FROM consolidated_trades WHERE trade_id=?",
                (TRADE,),
            ).fetchall()
        assert [(r["node_id"], r["pnl_dollars"]) for r in rows] == [
            ("peernode0001", pytest.approx(ESTIMATE))
        ]
