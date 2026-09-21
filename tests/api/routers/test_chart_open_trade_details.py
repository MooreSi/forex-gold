"""`GET /api/chart/trades` must actually carry the fields it promises.

`ChartTrade` declares `id`, `direction`, `entry`, `lots`, `sl` and `tp`, and
the engine has never produced any of them except `direction`: the rows come
straight out of `vantage_simulated_trades`, whose columns are `trade_id`,
`entry_price`, `lot_size`, `stop_loss` and `tp1`. Every declared field came
back null, so the chart's positions table rendered an em dash in every column
but Side, and `CandleChart` -- which filters on `typeof t.entry === "number"`
-- drew no entry marker at all.

Reported from the running app on 2026-09-21: "when there are open orders it
doesn't give the details, they are missing i.e. lot size". The schema said
extra="allow", so the real columns were travelling all along with nothing
reading them.

The names are mapped here, at the edge that promises them, rather than in the
engine: `get_open_trades` feeds several screens that already read the long
column names, and renaming there would break them to fix this.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import chart as chart_router

ROW = {
    "trade_id": "e221db83-e55d-4b", "mt5_ticket": 2050682687,
    "direction": "BUY", "entry_price": 4376.97, "lot_size": 0.1,
    "stop_loss": 4326.97, "tp1": 4406.97, "tp2": 4436.97,
    "status": "open", "tg_source": "Reversal Engine",
}


@pytest.fixture
def lab(monkeypatch, sentinel_engine):
    state = {"rows": [dict(ROW)]}

    async def _open(engine):
        return [dict(r) for r in state["rows"]]

    monkeypatch.setattr(chart_router.chart_ctl, "get_open_trades", _open)
    return state


def _first(client):
    return client.get("/api/chart/trades").json()[0]


class TestTheDetailsReachTheScreen:
    def test_the_lot_size_is_reported(self, make_client, lab):
        """The one the owner named."""
        assert _first(make_client())["lots"] == 0.1

    def test_the_entry_price_is_reported(self, make_client, lab):
        """Also what the chart's entry marker filters on, so without it the
        position is not drawn on the candles either."""
        assert _first(make_client())["entry"] == 4376.97

    def test_the_stop_is_reported(self, make_client, lab):
        assert _first(make_client())["sl"] == 4326.97

    def test_the_first_take_profit_is_reported(self, make_client, lab):
        """`tp` is a single value on this schema and a trade has up to eight.
        The first is the one the panel shows."""
        assert _first(make_client())["tp"] == 4406.97

    def test_the_direction_still_comes_through(self, make_client, lab):
        assert _first(make_client())["direction"] == "BUY"


class TestWhatItMustNotInvent:
    def test_a_trade_with_no_stop_reports_none_not_zero(self, make_client, lab):
        """A 0.00 stop reads as a stop at zero, which is a stop that would
        never be hit -- the most dangerous possible wrong answer here."""
        lab["rows"] = [{**ROW, "stop_loss": None}]

        assert _first(make_client())["sl"] is None

    def test_a_trade_with_no_take_profit_reports_none(self, make_client, lab):
        lab["rows"] = [{**ROW, "tp1": None}]

        assert _first(make_client())["tp"] is None

    def test_the_raw_columns_still_travel(self, make_client, lab):
        """extra="allow" is why the panel could be fixed at all; other screens
        read these names and must keep working."""
        body = _first(make_client())

        assert body["lot_size"] == 0.1
        assert body["tg_source"] == "Reversal Engine"

    def test_no_open_trades_is_an_empty_list(self, make_client, lab):
        lab["rows"] = []

        assert make_client().get("/api/chart/trades").json() == []
