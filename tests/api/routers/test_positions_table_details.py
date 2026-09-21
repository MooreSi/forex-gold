"""The Trading tab's Positions table must show the position, not five dashes.

Screenshotted by the owner on 2026-09-21: three open SELLs, and Lots, Entry,
SL and P&L all rendering an em dash. Same root cause as the chart's table --
the rows are `vantage_simulated_trades` columns and the UI reads short names
-- and one more consequence that does touch money: the Close button posts to
`/api/trading/trades/{id}/close` with `trade.id`, which was undefined on every
row, so closing from this table could not work either.

The owner also asked for the strategy and the source, which the rows have
always carried under `strategy` and `tg_source`.

The LABELS are decided by the backend, not the browser: `strategy` is stored
as `template:<name>` and `tg_source` holds an engine name, a channel name or a
marker like `manual_market`. Re-deriving those rules in TypeScript would be a
second answer to what a trade's source is.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import trading as trading_router

ROW = {
    "trade_id": "e221db83-e55d-4b", "mt5_ticket": 2050682687,
    "direction": "SELL", "entry_price": 4376.97, "lot_size": 0.1,
    "stop_loss": 4426.97, "tp1": 4346.97, "mt5_profit": -12.40,
    "strategy": "template:30 TP1 SL50 and Trail", "tg_source": "Reversal Engine",
    "status": "open",
}


@pytest.fixture
def lab(monkeypatch, sentinel_engine):
    state = {"rows": [dict(ROW)]}

    async def _open(engine):
        return [dict(r) for r in state["rows"]]

    monkeypatch.setattr(trading_router.trading_ctl, "get_open_trades", _open)
    return state


def _first(client):
    return client.get("/api/trading/trades").json()[0]


class TestTheNumbersThatWereDashes:
    def test_the_lot_size(self, make_client, lab):
        assert _first(make_client())["lots"] == 0.1

    def test_the_entry(self, make_client, lab):
        assert _first(make_client())["entry"] == 4376.97

    def test_the_stop(self, make_client, lab):
        assert _first(make_client())["sl"] == 4426.97

    def test_the_live_pnl(self, make_client, lab):
        """An open position's P&L is the broker's running number."""
        assert _first(make_client())["pnl"] == -12.40


class TestClosingCanIdentifyThePosition:
    def test_the_row_carries_the_id_the_close_route_wants(self, make_client, lab):
        """`POST /api/trading/trades/{trade_id}/close`. Without this the
        button posted the string "undefined"."""
        assert _first(make_client())["id"] == "e221db83-e55d-4b"

# The source and strategy LABELS are added by the service that reads the rows
# (`analytics/reporting.get_open_trades`), not by this route, so they are
# tested against it directly in
# tests/analytics/test_open_trades_carry_display_labels.py. Stubbing the
# controller here would test a fake.
