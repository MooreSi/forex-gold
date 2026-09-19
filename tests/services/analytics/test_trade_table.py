"""Closed trades, deal by deal, as the Analysis table shows them.

The last thing the React port could not build. The NiceGUI page read
`engine._bridge.get_deal_history()` directly, which the controller boundary
forbids; `get_deal_history` is on the runtime facade as of 2026-09-19, with the
allowlist raised 89 -> 90 and the reason recorded in `facade_baseline.json`.

**Built from MT5's own record, not the local ledger.** A trade opened by hand in
the terminal, or by the copier EA, never had a local row, and a table built from
the local ledger would omit it while looking complete. The local database is
still where the attribution comes from, and this merges the two.

Nothing here reaches a broker: the engine is a stub returning canned deals, and
every map is a dict.
"""
from __future__ import annotations

import time

import pytest

from backend.src.services.analytics import trade_table

pytestmark = pytest.mark.asyncio


class _Engine:
    """One facade method, which is all this service is allowed to use."""

    def __init__(self, deals=None, raises=None):
        self.deals = deals
        self.raises = raises
        self.calls: list[int] = []

    async def get_deal_history(self, days: int = 7):
        self.calls.append(days)
        if self.raises:
            raise self.raises
        return self.deals


def _deal(**over) -> dict:
    row = {"position_id": 100, "entry": 0, "type": 0, "price": 2400.0,
           "volume": 0.10, "time": 1_750_000_000.0, "comment": "", "profit": 0.0}
    row.update(over)
    return row


@pytest.fixture
def maps(monkeypatch):
    """Every attribution map, empty unless a test fills it."""
    state = {"source": {}, "strategy": {}, "max_tp": {}, "rr": {},
             "order_type": {}, "group": {}, "comments": ({}, {}, {}),
             "spreads": {}, "fee_rate": 0.0, "pnl": (12.5, 0.0)}

    async def _amap(key):
        async def _fn(*a, **k):
            return dict(state[key]) if isinstance(state[key], dict) else state[key]
        return _fn

    for name, key in (("source_map", "source"), ("strategy_map", "strategy"),
                      ("max_tp_map", "max_tp"), ("rr_map", "rr"),
                      ("order_type_map", "order_type"), ("group_map", "group")):
        async def _fn(*a, _key=key, **k):
            return dict(state[_key])
        monkeypatch.setattr(trade_table._maps, name, _fn)

    async def _comments(leg_comments):
        state["seen_comments"] = dict(leg_comments)
        return state["comments"]

    async def _fee_rate():
        return state["fee_rate"]

    monkeypatch.setattr(trade_table._maps, "comment_attribution_maps", _comments)
    monkeypatch.setattr(trade_table._fees, "platform_fee_rate", _fee_rate)
    monkeypatch.setattr(trade_table._fees, "apply_fee",
                        lambda *a, **k: state["pnl"])
    monkeypatch.setattr(trade_table._spreads, "get_cached_spreads",
                        lambda ids: dict(state["spreads"]))
    monkeypatch.setattr(trade_table._labels, "parse_reason",
                        lambda comment, pnl=0.0: comment or "closed")
    return state


class TestWhatCountsAsATrade:
    async def test_one_row_per_closed_position(self, maps):
        engine = _Engine([_deal(), _deal(entry=1, type=1, price=2410.0,
                                         time=1_750_003_600.0)])

        out = await trade_table.closed_trades(engine, 30)

        assert len(out["rows"]) == 1
        assert out["rows"][0]["ticket"] == 100

    async def test_a_position_with_no_close_yet_is_not_a_closed_trade(self, maps):
        """The open-positions panel owns those."""
        engine = _Engine([_deal()])

        out = await trade_table.closed_trades(engine, 30)

        # Not whole-dict equality: the payload gained a `curve` key on
        # 2026-09-19 and this test is about rows, not about the shape.
        assert out["rows"] == []
        assert out["error"] is None

    async def test_balance_operations_are_not_trades(self, maps):
        """A deposit has no position_id. Counting one as a trade would put a
        deposit in the P&L column."""
        engine = _Engine([_deal(position_id=None, entry=1),
                          _deal(position_id=0, entry=1)])

        out = await trade_table.closed_trades(engine, 30)

        assert out["rows"] == []

    async def test_a_trade_opened_before_the_window_is_still_reported(self, maps):
        """Its opening deal is outside the range. Dropping it would make a
        long-running trade vanish from the day it closed."""
        engine = _Engine([_deal(entry=1, type=1, price=2410.0)])

        rows = (await trade_table.closed_trades(engine, 30))["rows"]

        assert len(rows) == 1
        assert rows[0]["entry_price"] == 0.0

    async def test_that_trade_s_direction_is_inferred_from_the_close(self, maps):
        """A position closed by a sell was a buy. Reporting the close's own
        side would show every one of them backwards."""
        engine = _Engine([_deal(entry=1, type=0)])

        assert (await trade_table.closed_trades(engine, 30))["rows"][0]["direction"] == "SELL"

    async def test_the_window_is_forwarded_to_the_broker(self, maps):
        engine = _Engine([])

        await trade_table.closed_trades(engine, 90)

        assert engine.calls == [90]


class TestAPartialCloseIsSeveralDeals:
    async def test_every_exit_is_reported(self, maps):
        """The lots column shows the breakdown, which the browser cannot
        reconstruct from one number."""
        engine = _Engine([
            _deal(volume=0.10),
            _deal(entry=1, type=1, volume=0.04, time=1_750_001_000.0),
            _deal(entry=1, type=1, volume=0.06, time=1_750_002_000.0),
        ])

        row = (await trade_table.closed_trades(engine, 30))["rows"][0]

        assert row["lots"] == pytest.approx(0.10)
        assert row["close_lots"] == [0.04, 0.06]

    async def test_the_last_exit_is_the_closing_one(self, maps):
        """Not the first. The close price and time come from the deal that
        actually finished the position."""
        engine = _Engine([
            _deal(),
            _deal(entry=1, type=1, price=2405.0, time=1_750_001_000.0),
            _deal(entry=1, type=1, price=2415.0, time=1_750_002_000.0),
        ])

        row = (await trade_table.closed_trades(engine, 30))["rows"][0]

        assert row["exit_price"] == 2415.0
        assert row["close_ts"] == 1_750_002_000.0


class TestTheNumbers:
    async def test_pips_follow_the_direction(self, maps):
        buy = _Engine([_deal(), _deal(entry=1, type=1, price=2410.0)])
        sell = _Engine([_deal(type=1), _deal(entry=1, type=0, price=2410.0)])

        assert (await trade_table.closed_trades(buy, 30))["rows"][0]["pips"] == \
            pytest.approx(100.0)
        assert (await trade_table.closed_trades(sell, 30))["rows"][0]["pips"] == \
            pytest.approx(-100.0)

    async def test_pips_are_none_when_there_is_no_entry_price(self, maps):
        """Not zero. A trade opened outside the window has an unknown entry,
        and 0.0 pips reads as a scratch."""
        engine = _Engine([_deal(entry=1, type=1, price=2410.0)])

        assert (await trade_table.closed_trades(engine, 30))["rows"][0]["pips"] is None

    async def test_duration_is_none_rather_than_zero_without_an_open(self, maps):
        engine = _Engine([_deal(entry=1, type=1)])

        assert (await trade_table.closed_trades(engine, 30))["rows"][0]["duration_secs"] is None

    async def test_a_cached_spread_replaces_the_fee_figure(self, maps):
        """Vantage Standard STP is spread-only: the spread IS the cost, and it
        is already in the P&L via the real fill prices. Showing a commission
        beside it would imply a second deduction."""
        maps["spreads"] = {100: {"spread_points": 2.4, "spread_cost_usd": 2.40}}
        engine = _Engine([_deal(), _deal(entry=1, type=1)])

        row = (await trade_table.closed_trades(engine, 30))["rows"][0]

        assert row["fees"] == pytest.approx(2.40)
        assert row["spread_points"] == pytest.approx(2.4)

    async def test_an_uncached_spread_is_none_not_zero(self, maps):
        """0.0 points would read as a zero-spread fill, which does not happen."""
        engine = _Engine([_deal(), _deal(entry=1, type=1)])

        assert (await trade_table.closed_trades(engine, 30))["rows"][0]["spread_points"] is None


class TestTheMaxTpColumn:
    """Three states, not two. A blank and a "none" mean different things, and
    an operator reading "none" too early would conclude a trade never went
    their way when nothing has looked yet."""

    async def test_a_computed_result_is_shown(self, maps):
        maps["max_tp"] = {"100": "TP3"}
        engine = _Engine([_deal(), _deal(entry=1, type=1)])

        assert (await trade_table.closed_trades(engine, 30))["rows"][0]["max_tp"] == "TP3"

    async def test_a_recent_close_with_no_result_is_blank(self, maps):
        engine = _Engine([_deal(time=time.time()),
                          _deal(entry=1, type=1, time=time.time())])

        assert (await trade_table.closed_trades(engine, 30))["rows"][0]["max_tp"] == ""

    async def test_an_old_close_with_no_result_says_it_is_pending(self, maps):
        engine = _Engine([_deal(), _deal(entry=1, type=1, time=1.0)])

        assert (await trade_table.closed_trades(engine, 30))["rows"][0]["max_tp"] == "..."


class TestAttribution:
    async def test_the_local_maps_are_merged_on(self, maps):
        maps["source"] = {"100": "GoldSignals"}
        maps["strategy"] = {"100": "Scale out"}
        maps["rr"] = {"100": 2.5}
        maps["group"] = {"100": ("Grid A", 2)}
        engine = _Engine([_deal(), _deal(entry=1, type=1)])

        row = (await trade_table.closed_trades(engine, 30))["rows"][0]

        assert row["source"] == "GoldSignals"
        assert row["strategy"] == "Scale out"
        assert row["rr"] == 2.5
        assert row["group"] == ["Grid A", 2]

    async def test_a_position_with_no_local_row_is_attributed_from_its_comment(
        self, maps,
    ):
        """An EA-template sibling leg, or the copier EA, never had a local row.
        Without this they appear with no channel and no strategy at all."""
        maps["comments"] = ({"100": "GD VIP"}, {"100": "Limit runner"}, {})
        engine = _Engine([_deal(comment="sig:abc"), _deal(entry=1, type=1)])

        row = (await trade_table.closed_trades(engine, 30))["rows"][0]

        assert row["source"] == "GD VIP"
        assert row["strategy"] == "Limit runner"

    async def test_a_real_local_row_always_wins_over_an_inference(self, maps):
        """setdefault, not update. A comment is a guess; a local row is a
        record."""
        maps["source"] = {"100": "GoldSignals"}
        maps["comments"] = ({"100": "GUESSED"}, {}, {})
        engine = _Engine([_deal(comment="sig:abc"), _deal(entry=1, type=1)])

        assert (await trade_table.closed_trades(engine, 30))["rows"][0]["source"] == \
            "GoldSignals"

    async def test_attribution_failing_does_not_lose_the_table(self, maps, monkeypatch):
        """The rows are the point. A missing channel name is a blank cell; an
        exception is an empty screen."""
        async def _boom(_c):
            raise RuntimeError("no ledger")

        monkeypatch.setattr(trade_table._maps, "comment_attribution_maps", _boom)
        engine = _Engine([_deal(comment="sig:abc"), _deal(entry=1, type=1)])

        assert len((await trade_table.closed_trades(engine, 30))["rows"]) == 1


class TestWhenTheBrokerCannotAnswer:
    async def test_it_is_reported_as_an_error_not_an_empty_table(self, maps):
        """"No trades in this window" and "the bridge is down" look identical
        in an empty table and call for completely different responses."""
        engine = _Engine(raises=RuntimeError("bridge offline"))

        out = await trade_table.closed_trades(engine, 30)

        assert out["rows"] == []
        assert "bridge offline" in out["error"]

    async def test_none_from_the_bridge_is_also_an_error(self, maps):
        """`get_deal_history` returns None when it cannot look, which is not
        the same as returning no deals."""
        engine = _Engine(None)

        assert (await trade_table.closed_trades(engine, 30))["error"]

    async def test_genuinely_no_trades_is_not_an_error(self, maps):
        engine = _Engine([])

        out = await trade_table.closed_trades(engine, 30)

        assert out["rows"] == []
        assert out["error"] is None


async def test_the_newest_close_is_first(maps):
    """A table an operator scans from the top wants today at the top."""
    engine = _Engine([
        _deal(position_id=1), _deal(position_id=1, entry=1, type=1, time=100.0),
        _deal(position_id=2), _deal(position_id=2, entry=1, type=1, time=900.0),
    ])

    rows = (await trade_table.closed_trades(engine, 30))["rows"]

    assert [r["ticket"] for r in rows] == [2, 1]


class TestTheEquityCurveRidesAlong:
    """The curve is built from the rows this read already produced.

    Added 2026-09-19 with the Analysis tab's equity curve. Its own arithmetic
    is tested in test_equity_curve.py; what matters here is that it comes from
    THIS read -- a second fetch against a moving account is a second chance
    for the curve and the table beneath it to disagree on screen.
    """

    async def test_a_successful_read_carries_a_curve(self, maps):
        engine = _Engine([
            _deal(position_id=771, entry=0, type=0, price=2400.0, time=100),
            _deal(position_id=771, entry=1, type=1, price=2410.0, time=200),
        ])

        out = await trade_table.closed_trades(engine, 7)

        assert out["curve"]["trades"] == len(out["rows"])

    async def test_the_curve_is_present_and_empty_when_the_bridge_is_down(self, maps):
        # Absent would be a crash in the browser, which reads curve.points
        # before it reads error.
        engine = _Engine(raises=RuntimeError("bridge is down"))

        out = await trade_table.closed_trades(engine, 7)

        assert out["curve"] == {"points": [], "net": 0.0, "peak": 0.0,
                                "max_drawdown": 0.0, "trades": 0}
