"""What the Positions table shows, and the two things it was not showing.

Both reported by the owner, 2026-09-21:

* **"current positions do not show the current p&l"**. They could not. The
  Positions table's P&L column reads `mt5_profit`, and that column is only
  written when a trade CLOSES -- `record_close`, the history importer and the
  profit sync all write it after the fact. While a position is open the column
  is null, so every row showed an em dash.

* **"it is showing one position when on mt5 there are two"**. The table is
  built from `vantage_simulated_trades WHERE status='open'` -- the app's own
  record. A position opened by hand in MetaTrader, or one whose record was
  lost, is invisible here while being very much open at the broker.

This view answers both from the broker's live `/positions` payload, which
already carries a running `profit` per ticket.

**The broker's number wins, and the stored one is never overwritten.** The
running P&L is placed on the row for display; `mt5_profit` in the database
stays the realised figure it has always been, because closing, reporting and
the balance report all read it and a running number there would corrupt them.

**An untracked position is shown and is NOT closable from here.** It has no
`trade_id`, so the Close button would post to
`/api/trading/trades/undefined/close`; and closing a position the app has no
record of is a money action with no record to update. It is flagged, and the
frontend disables the control.
"""
from __future__ import annotations

import pytest

from backend.src.services.positions import live_view


class FakeBridge:
    def __init__(self, positions, configured=True):
        self._positions = positions
        self._configured = configured
        self.calls = 0

    def is_configured(self) -> bool:
        return self._configured

    async def get_positions(self):
        self.calls += 1
        return self._positions


def _tracked(**over):
    row = {
        "trade_id": "t-1", "mt5_ticket": 111, "direction": "BUY",
        "lot_size": 0.10, "entry_price": 2650.0, "stop_loss": 2640.0,
        "tp1": 2670.0, "mt5_profit": None, "status": "open",
        "strategy_label": "Scalp", "source_label": "GoldSignals",
    }
    row.update(over)
    return row


def _live(**over):
    p = {
        "ticket": 111, "symbol": "XAUUSD", "type": "BUY", "volume": 0.10,
        "open_price": 2650.0, "current_price": 2655.0, "sl": 2640.0,
        "tp": 2670.0, "profit": 50.0, "swap": -1.5, "open_time": 1_700_000_000,
        "comment": "ForexTrader",
    }
    p.update(over)
    return p


# ── Running P&L on a tracked position ────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_tracked_position_gets_the_brokers_running_pnl():
    rows = await live_view.build([_tracked()], FakeBridge([_live()]))

    assert rows[0]["pnl"] == pytest.approx(48.5)  # profit 50.00 + swap -1.50


@pytest.mark.asyncio
async def test_the_running_pnl_does_not_overwrite_the_stored_column():
    """`mt5_profit` is the REALISED figure. record_close, the balance report
    and the history importer all read it; a running number there is corruption,
    not a display change."""
    rows = await live_view.build([_tracked(mt5_profit=None)], FakeBridge([_live()]))

    assert rows[0]["mt5_profit"] is None


@pytest.mark.asyncio
async def test_a_position_the_broker_does_not_report_keeps_a_null_pnl():
    """An em dash is the honest answer. A zero reads as break-even, which is a
    number the operator would act on."""
    rows = await live_view.build([_tracked(mt5_ticket=999)], FakeBridge([_live()]))

    assert rows[0]["pnl"] is None


@pytest.mark.asyncio
async def test_an_unreachable_bridge_leaves_the_rows_alone():
    """The app's own record is still worth showing when the broker cannot be
    reached. Losing the table entirely would be worse than losing one column."""
    rows = await live_view.build([_tracked()], FakeBridge(None, configured=False))

    assert len(rows) == 1 and rows[0]["trade_id"] == "t-1"
    assert rows[0]["pnl"] is None


# ── The position that was missing ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_position_open_at_the_broker_with_no_record_is_shown():
    rows = await live_view.build([_tracked()], FakeBridge([_live(), _live(ticket=222)]))

    assert len(rows) == 2
    assert [r["mt5_ticket"] for r in rows] == [111, 222]


@pytest.mark.asyncio
async def test_the_untracked_row_carries_the_columns_the_table_reads():
    rows = await live_view.build([], FakeBridge([
        _live(ticket=222, type="SELL", volume=0.25, open_price=2700.0,
              sl=2710.0, tp=2680.0, profit=-12.0, swap=0.0),
    ]))

    row = rows[0]
    assert row["direction"] == "SELL"
    assert row["lot_size"] == 0.25
    assert row["entry_price"] == 2700.0
    assert row["stop_loss"] == 2710.0
    assert row["tp1"] == 2680.0
    assert row["pnl"] == pytest.approx(-12.0)


@pytest.mark.asyncio
async def test_an_untracked_row_is_flagged_and_has_no_trade_id():
    """No trade_id means the Close button cannot post a URL at all, which is
    the backstop behind the flag the frontend reads."""
    rows = await live_view.build([], FakeBridge([_live(ticket=222)]))

    assert rows[0]["untracked"] is True
    assert rows[0].get("trade_id") is None


@pytest.mark.asyncio
async def test_a_tracked_row_is_not_flagged_untracked():
    rows = await live_view.build([_tracked()], FakeBridge([_live()]))

    assert rows[0]["untracked"] is False


@pytest.mark.asyncio
async def test_the_untracked_row_says_where_it_came_from():
    """"—" in the Source column for a position that appeared out of nowhere is
    the least useful thing this table could say about it."""
    rows = await live_view.build([], FakeBridge([_live(ticket=222)]))

    assert "MT5" in rows[0]["source_label"]


# ── Not making things worse ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_bridge_is_asked_once():
    """One call per poll. This runs every few seconds on the Positions tab."""
    bridge = FakeBridge([_live()])
    await live_view.build([_tracked(), _tracked(trade_id="t-2", mt5_ticket=112)], bridge)

    assert bridge.calls == 1


@pytest.mark.asyncio
async def test_a_bridge_that_raises_does_not_take_the_table_down():
    class Exploding(FakeBridge):
        async def get_positions(self):
            raise RuntimeError("bridge went away")

    rows = await live_view.build([_tracked()], Exploding([]))

    assert len(rows) == 1 and rows[0]["pnl"] is None


@pytest.mark.asyncio
async def test_a_position_with_a_ticket_that_is_not_a_number_is_not_matched():
    """Tickets arrive as ints from MT5 and as whatever SQLite stored from the
    database. A crash on a malformed one would empty the whole table."""
    rows = await live_view.build([_tracked(mt5_ticket="")], FakeBridge([_live()]))

    assert rows[0]["pnl"] is None
    # ...and the live position is then untracked, because nothing claimed it.
    assert len(rows) == 2
