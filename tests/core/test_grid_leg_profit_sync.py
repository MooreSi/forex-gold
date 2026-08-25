"""A grid template's sibling legs' profit must count.

Root cause (found live 2026-07-30): an EA Template trade opens one broker
position per Anchor/Grid leg, but Python keeps a single vantage_simulated_trades
row per trade, and core_profit_sync.sync_profit only ever summed the ONE
ticket that promoted that row. A 3-leg grid's anchor alone showed $30.63 net
while its two sibling legs -- each a real, separate broker position -- were
never counted anywhere: not in net_pnl, not in the simulated balance
correction, not in any "this is what the signal made" figure.

Three pieces:
  * ea_bridge.find_template_leg_tickets -- the shared sibling-discovery lookup
    (extracted from a pattern reversal_engine_manage.py already used privately)
  * core_profit_sync.sync_profit -- now sums every discovered leg, deferring
    the final mt5_profit lock until none of them are still open, so a
    later-closing sibling still gets counted instead of being permanently lost
  * history.py's _template_group_map -- collapses grid siblings into one row
    the same way Adaptive Runner ladder legs already collapse, instead of N
    unrelated-looking rows with no total anywhere
"""
import asyncio
import os
import tempfile
import time

import pytest

from backend.src.services.trading import profit_sync as core_profit_sync
from backend.src.db import database as db
from backend.src.services.trading.sim_account import get_sim_account
from backend.src.services.broker.ea_bridge import comment_for_trade, find_template_leg_tickets


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


def _insert_trade(trade_id, mt5_ticket, strategy="template:Sig Gen Grid",
                  net_pnl=0.0, mt5_profit=None):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", "BUY", 4000.0, 4000.0, 3990.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time, net_pnl, mt5_profit, strategy) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", mt5_ticket, "BUY", 4000.0, 4000.0, 4000.0, 0.03,
             0.0, 3990.0, "closed", time.time(), net_pnl, mt5_profit, strategy),
        )


def _trade_dict(trade_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?",
                        (trade_id,)).fetchone()
        )


class _FakeBridge:
    """deals: {ticket: [deal, ...]}. positions: [{"ticket": n}, ...] still open."""

    def __init__(self, deals: dict, positions: list | None = None):
        self._deals = deals
        self._positions = positions or []
        self.deal_history_calls = 0

    async def get_position_history(self, ticket):
        return self._deals.get(int(ticket), [])

    async def get_deal_history(self, days):
        self.deal_history_calls += 1
        out = []
        for ticket, deals in self._deals.items():
            for d in deals:
                out.append({**d, "position_id": ticket})
        return out

    async def get_positions(self):
        return self._positions


# ── find_template_leg_tickets ────────────────────────────────────────────

def test_finds_every_leg_from_matching_comments():
    trade_id = "5b88a61e-6544-4f"
    comment = comment_for_trade(trade_id)

    class _B:
        async def get_deal_history(self, days):
            return [
                {"entry": 0, "comment": f"{comment}a1", "position_id": 100},
                {"entry": 0, "comment": f"{comment}g2", "position_id": 101},
                {"entry": 0, "comment": f"{comment}g3", "position_id": 102},
                {"entry": 0, "comment": "unrelated", "position_id": 999},
                {"entry": 1, "comment": f"{comment}a1", "position_id": 100},  # a close deal, ignored
            ]

    legs = asyncio.run(find_template_leg_tickets(trade_id, _B()))
    assert legs == {100, 101, 102}


def test_empty_for_a_non_template_trade_id():
    """No sibling lookup should even attempt to run for a trade that was
    never opened as a template -- there is no comment prefix to match."""
    class _B:
        async def get_deal_history(self, days):
            return [{"entry": 0, "comment": "unrelated", "position_id": 1}]

    legs = asyncio.run(find_template_leg_tickets("t-1", _B()))
    assert legs == set()


def test_degrades_to_empty_set_on_bridge_failure():
    class _B:
        async def get_deal_history(self, days):
            raise ConnectionError("bridge down")

    legs = asyncio.run(find_template_leg_tickets("5b88a61e-6544-4f", _B()))
    assert legs == set()


# ── core_profit_sync.sync_profit: multi-leg summing ──────────────────────

def test_sums_profit_across_every_closed_sibling_leg(fresh_db):
    """The regression itself: a 3-leg grid must report the sum of all three
    legs, not just the anchor's own ticket."""
    trade_id = "5b88a61e-6544-4f"
    comment = comment_for_trade(trade_id)
    _insert_trade(trade_id, mt5_ticket=100, net_pnl=0.0)
    deals = {
        100: [{"entry": 0, "comment": f"{comment}a1"},
              {"entry": 1, "profit": 30.63, "swap": 0.0, "fee": 0.0}],
        101: [{"entry": 0, "comment": f"{comment}g2"},
              {"entry": 1, "profit": 5.0, "swap": 0.0, "fee": 0.0}],
        102: [{"entry": 0, "comment": f"{comment}g3"},
              {"entry": 1, "profit": -2.0, "swap": -0.5, "fee": 0.0}],
    }
    bridge = _FakeBridge(deals)
    result = asyncio.run(core_profit_sync.sync_profit(trade_id, 100, bridge))
    assert result == pytest.approx(33.13)
    row = _trade_dict(trade_id)
    assert row["mt5_profit"] == pytest.approx(33.13)
    assert row["net_pnl"] == pytest.approx(33.13)


def test_non_template_trade_is_unaffected(fresh_db):
    """A trade whose strategy is not a template must never trigger sibling
    discovery -- confirms the multi-leg path is opt-in, not the new default
    for every trade."""
    _insert_trade("t-1", mt5_ticket=555, strategy="scale_out", net_pnl=10.0)
    bridge = _FakeBridge({555: [{"entry": 0}, {"entry": 1, "profit": 20.0, "swap": 0.0, "fee": -1.0}]})
    result = asyncio.run(core_profit_sync.sync_profit("t-1", 555, bridge))
    assert result == 19.0
    assert bridge.deal_history_calls == 0  # get_position_history alone answered it


def test_defers_settling_while_a_sibling_leg_is_still_open(fresh_db):
    """A sibling that hasn't closed yet must hold the trade open rather than
    letting whatever closed first freeze the total permanently."""
    trade_id = "5b88a61e-6544-4f"
    comment = comment_for_trade(trade_id)
    _insert_trade(trade_id, mt5_ticket=100, net_pnl=0.0)
    deals = {
        100: [{"entry": 0, "comment": f"{comment}a1"},
              {"entry": 1, "profit": 30.63, "swap": 0.0, "fee": 0.0}],
        101: [{"entry": 0, "comment": f"{comment}g2"}],  # opened, not yet closed
    }
    bridge = _FakeBridge(deals, positions=[{"ticket": 101}])
    result = asyncio.run(core_profit_sync.sync_profit(trade_id, 100, bridge))
    assert result is None                    # not settled -- schedule_profit_sync must keep retrying
    row = _trade_dict(trade_id)
    assert row["mt5_profit"] is None         # the "stop retrying" marker stays unset
    assert row["net_pnl"] == pytest.approx(30.63)   # but the known-so-far total is recorded


def test_a_later_closing_sibling_adds_to_the_total_without_double_counting(fresh_db):
    """Two calls, as the real sequence happens: the anchor closes and syncs
    first, then a sibling closes later and a retry (schedule_profit_sync /
    profit_sweep) picks it up. The second call's balance correction must be
    exactly the NEW leg's money, not the whole total re-applied."""
    trade_id = "5b88a61e-6544-4f"
    comment = comment_for_trade(trade_id)
    _insert_trade(trade_id, mt5_ticket=100, net_pnl=0.0)
    starting_balance = get_sim_account()["balance"]

    deals = {
        100: [{"entry": 0, "comment": f"{comment}a1"},
              {"entry": 1, "profit": 30.63, "swap": 0.0, "fee": 0.0}],
        101: [{"entry": 0, "comment": f"{comment}g2"}],  # still open
    }
    bridge = _FakeBridge(deals, positions=[{"ticket": 101}])
    first = asyncio.run(core_profit_sync.sync_profit(trade_id, 100, bridge))
    assert first is None
    assert get_sim_account()["balance"] == pytest.approx(starting_balance + 30.63)

    # The sibling now closes.
    bridge._deals[101].append({"entry": 1, "profit": 5.0, "swap": 0.0, "fee": 0.0})
    bridge._positions = []
    second = asyncio.run(core_profit_sync.sync_profit(trade_id, 100, bridge))
    assert second == pytest.approx(35.63)
    assert get_sim_account()["balance"] == pytest.approx(starting_balance + 35.63)
    assert _trade_dict(trade_id)["mt5_profit"] == pytest.approx(35.63)


def test_sibling_lookup_failure_falls_back_to_the_anchor_alone(fresh_db):
    """A broken leg lookup must degrade to the previous single-ticket
    behaviour, not abort the sync entirely."""
    _insert_trade("5b88a61e-6544-4f", mt5_ticket=100, net_pnl=0.0)

    class _BrokenBridge(_FakeBridge):
        async def get_deal_history(self, days):
            raise ConnectionError("bridge down")

    deals = {100: [{"entry": 0}, {"entry": 1, "profit": 30.63, "swap": 0.0, "fee": 0.0}]}
    bridge = _BrokenBridge(deals)
    result = asyncio.run(core_profit_sync.sync_profit("5b88a61e-6544-4f", 100, bridge))
    assert result == pytest.approx(30.63)


# ── history.py's grouping ─────────────────────────────────────────────────

def test_template_group_map_assigns_anchor_to_tier_one(fresh_db):
    """The anchor is tier 1 by which ticket promoted the local row, not by
    ticket number -- here the anchor's ticket (101) is not the lowest."""
    # Moved to the analytics repo when the
    # frontend-never-imports-the-database contract was restored (2026-08-25).
    from backend.src.services.analytics.trade_history_repo import _template_group_map

    trade_id = "5b88a61e-6544-4f"
    comment = comment_for_trade(trade_id)
    _insert_trade(trade_id, mt5_ticket=101)
    leg_comments = {
        "101": f"{comment}a1",
        "100": f"{comment}g2",
        "102": f"{comment}g3",
    }
    group_map = _template_group_map(leg_comments)
    assert group_map["101"] == (trade_id, 1)
    assert group_map["100"] == (trade_id, 2)
    assert group_map["102"] == (trade_id, 3)


def test_template_group_map_ignores_a_lone_leg(fresh_db):
    """A prefix with only one resolved ticket is not a group -- nothing to
    collapse, same rule _ticket_group_map's own caller already applies."""
    # Moved to the analytics repo when the
    # frontend-never-imports-the-database contract was restored (2026-08-25).
    from backend.src.services.analytics.trade_history_repo import _template_group_map

    trade_id = "5b88a61e-6544-4f"
    _insert_trade(trade_id, mt5_ticket=101)
    leg_comments = {"101": f"{comment_for_trade(trade_id)}a1"}
    assert _template_group_map(leg_comments) == {}


def test_template_group_map_ignores_a_prefix_with_no_local_row(fresh_db):
    """No trade row for the prefix (e.g. it was never promoted, or the DB
    row was pruned) means nothing to attach the group to -- must not raise
    or invent a group id."""
    # Moved to the analytics repo when the
    # frontend-never-imports-the-database contract was restored (2026-08-25).
    from backend.src.services.analytics.trade_history_repo import _template_group_map

    comment = comment_for_trade("nosuchrow12")
    leg_comments = {"1": f"{comment}a1", "2": f"{comment}g2"}
    assert _template_group_map(leg_comments) == {}


# ── initial_risk refinement (2026-08-07) ─────────────────────────────────
# net_pnl covers every leg, so the R:R denominator has to as well. The row's
# open-time seed had to assume every staged leg fills at the anchor's price;
# each leg's own opening deal is what actually resolves it.


def _insert_trade_with_initial_sl(trade_id, mt5_ticket, initial_sl, seed_risk,
                                  strategy="template:Sig Gen Grid"):
    _insert_trade(trade_id, mt5_ticket=mt5_ticket, strategy=strategy)
    with db.db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET initial_sl=?, initial_risk=? "
            "WHERE trade_id=?",
            (initial_sl, seed_risk, trade_id),
        )


def test_initial_risk_is_recomputed_from_every_filled_leg(fresh_db):
    """Two legs filling at different prices against the shared stop of 3990:
    the anchor at 4000 risks 10.00 * 0.10 * 100 = $100, the pending leg
    filling 4 better at 3996 risks only $60. The seed written at open
    assumed both at the anchor's price ($200)."""
    trade_id = "5b88a61e-6544-4f"
    comment = comment_for_trade(trade_id)
    _insert_trade_with_initial_sl(trade_id, mt5_ticket=100,
                                  initial_sl=3990.0, seed_risk=200.0)
    deals = {
        100: [{"entry": 0, "comment": f"{comment}a1", "price": 4000.0, "volume": 0.10},
              {"entry": 1, "profit": 30.0, "swap": 0.0, "fee": 0.0}],
        101: [{"entry": 0, "comment": f"{comment}g2", "price": 3996.0, "volume": 0.10},
              {"entry": 1, "profit": 10.0, "swap": 0.0, "fee": 0.0}],
    }
    asyncio.run(core_profit_sync.sync_profit(trade_id, 100, _FakeBridge(deals)))
    assert _trade_dict(trade_id)["initial_risk"] == pytest.approx(160.0)


def test_a_pending_leg_that_never_filled_carries_no_risk(fresh_db):
    """The seed counts every leg the EA staged, but a resting leg that
    expires unfilled never took on risk -- it must not inflate the
    denominator of a trade the anchor alone actually ran."""
    trade_id = "5b88a61e-6544-4f"
    comment = comment_for_trade(trade_id)
    _insert_trade_with_initial_sl(trade_id, mt5_ticket=100,
                                  initial_sl=3990.0, seed_risk=200.0)
    deals = {
        100: [{"entry": 0, "comment": f"{comment}a1", "price": 4000.0, "volume": 0.10},
              {"entry": 1, "profit": -98.0, "swap": 0.0, "fee": 0.0}],
    }
    asyncio.run(core_profit_sync.sync_profit(trade_id, 100, _FakeBridge(deals)))
    row = _trade_dict(trade_id)
    assert row["initial_risk"] == pytest.approx(100.0)
    # -$98 against the one leg's real $100 risk is the -0.98R it actually was,
    # not the -0.49R the two-leg seed would have shown.
    assert row["net_pnl"] / row["initial_risk"] == pytest.approx(-0.98)


def test_rerunning_the_sync_does_not_accumulate_risk(fresh_db):
    """initial_risk is written as an absolute, not an increment -- sync_profit
    is retried on a schedule while later legs settle."""
    trade_id = "5b88a61e-6544-4f"
    comment = comment_for_trade(trade_id)
    _insert_trade_with_initial_sl(trade_id, mt5_ticket=100,
                                  initial_sl=3990.0, seed_risk=200.0)
    deals = {
        100: [{"entry": 0, "comment": f"{comment}a1", "price": 4000.0, "volume": 0.10},
              {"entry": 1, "profit": 30.0, "swap": 0.0, "fee": 0.0}],
    }
    bridge = _FakeBridge(deals)
    asyncio.run(core_profit_sync.sync_profit(trade_id, 100, bridge))
    asyncio.run(core_profit_sync.sync_profit(trade_id, 100, bridge))
    assert _trade_dict(trade_id)["initial_risk"] == pytest.approx(100.0)


def test_a_row_with_no_initial_sl_is_left_alone(fresh_db):
    """Rows opened before the column existed have nothing to measure a
    distance from -- the seed must not be overwritten with a bogus figure
    computed against a NULL stop."""
    _insert_trade("t-old", mt5_ticket=555, strategy="scale_out", net_pnl=10.0)
    deals = {555: [{"entry": 0, "price": 4000.0, "volume": 0.10},
                   {"entry": 1, "profit": 20.0, "swap": 0.0, "fee": 0.0}]}
    asyncio.run(core_profit_sync.sync_profit("t-old", 555, _FakeBridge(deals)))
    assert _trade_dict("t-old")["initial_risk"] is None
