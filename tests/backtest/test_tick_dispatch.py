"""The backtest walks EA templates over real ticks when ticks are supplied.

Phase 1 of docs/todo/backtest/010 -- run_backtest_ticks()/_simulate_ticks()
mirror run_backtest()/_simulate()'s dispatch, but resolve fills and every
SL/TP/trail check against actual bid/ask instead of a bar's high/low.
"""
from __future__ import annotations

import pathlib

import pytest

from backend.src.services.backtest import engine as bt


def _sig(**over):
    base = dict(
        signal_id="s1", direction="BUY", entry_low=3999.5, entry_high=4000.5,
        stop_loss=3995.0, tp1=4002.0, tp2=None, tp3=None, created_ts=0.0,
    )
    base.update(over)
    return bt.BtSignal(**base)


# Unlike candles, tick "time" from the bridge is already true UTC (see
# _simulate_ticks's comment) -- no _BROKER_TZ_OFFSET needed here.
_TS0 = 20_000.0


def _ticks(*bids, spread=0.02) -> list[dict]:
    return [{"time": _TS0 + i, "bid": b, "ask": b + spread}
            for i, b in enumerate(bids)]


def _tpl(**over) -> dict:
    base = {
        "name": "Sim Me", "mode": "single", "lot_anchor": 0.10, "risk_pct": 0.0,
        "sl_pips": 50.0, "tpsl_mode": "on", "partials": 1,
        "close_full_on_last": 1, "be_mode": "entry", "be_trigger": 0,
        "trail_mode": "off", "tp1_pips": 20.0, "tp1_pct": 100.0,
    }
    base.update(over)
    return base


@pytest.fixture
def templates(monkeypatch):
    store: dict = {"Sim Me": _tpl()}
    monkeypatch.setattr(bt, "_load_backtest_template",
                        lambda name: store.get(name))
    return store


class TestTheDispatch:
    def test_a_template_strategy_is_simulated(self, templates):
        stats = bt.run_backtest_ticks(
            [_sig()], _ticks(4000.0, 4002.5), ["template:Sim Me"],
            starting_balance=10_000.0, spread_pts=0.0)

        assert stats["template:Sim Me"].trades == 1

    def test_it_uses_the_template_anchor_lot(self, templates):
        templates["Sim Me"] = _tpl(lot_anchor=0.07)

        stats = bt.run_backtest_ticks(
            [_sig()], _ticks(4000.0, 4002.5), ["template:Sim Me"],
            starting_balance=10_000.0, spread_pts=0.0)

        assert stats["template:Sim Me"].trade_list[0].lot_size == pytest.approx(0.07)

    def test_a_grid_template_is_never_simulated(self, templates):
        templates["Sim Me"] = _tpl(mode="grid")

        stats = bt.run_backtest_ticks(
            [_sig()], _ticks(4000.0, 4002.5), ["template:Sim Me"],
            starting_balance=10_000.0, spread_pts=0.0)

        assert stats["template:Sim Me"].trades == 0

    def test_a_missing_template_produces_no_trades(self, templates):
        stats = bt.run_backtest_ticks(
            [_sig()], _ticks(4000.0, 4002.5), ["template:No Such Template"],
            starting_balance=10_000.0, spread_pts=0.0)

        assert stats["template:No Such Template"].trades == 0


class TestFillDetection:
    def test_fills_when_mid_enters_the_zone(self, templates):
        """entry_low=3999.5, entry_high=4000.5 -- the first tick already
        sits inside the zone, so it should fill on tick 0."""
        stats = bt.run_backtest_ticks(
            [_sig()], _ticks(4000.0, 4002.5), ["template:Sim Me"],
            starting_balance=10_000.0, spread_pts=0.0)

        assert stats["template:Sim Me"].trades == 1

    def test_does_not_fill_before_price_reaches_the_zone(self, templates):
        """Every tick stays above the zone (4001+) -- no fill, no trade."""
        stats = bt.run_backtest_ticks(
            [_sig()], _ticks(4005.0, 4006.0, 4007.0), ["template:Sim Me"],
            starting_balance=10_000.0, spread_pts=0.0)

        assert stats["template:Sim Me"].trades == 0

    def test_a_tick_before_the_signal_was_created_is_ignored(self, templates):
        """Tick 0 already sits in the zone, but the signal was not created
        until tick 3 -- the fill must be found at tick 3, not tick 0, or a
        signal would fill against price history from before it existed."""
        sig = _sig(created_ts=_TS0 + 3)
        ticks = _ticks(4000.0, 4000.0, 4000.0, 4000.0)

        stats = bt.run_backtest_ticks(
            [sig], ticks, ["template:Sim Me"],
            starting_balance=10_000.0, spread_pts=0.0)

        trade = stats["template:Sim Me"].trade_list[0]
        assert trade.fill_bar_idx == 3


class TestBuiltInsAreNotGuessedAt:
    def test_a_non_template_strategy_produces_no_trades(self):
        """The picker only ever offers templates; a built-in key reaching
        here would be a picker bug, not something to approximate."""
        stats = bt.run_backtest_ticks(
            [_sig()], _ticks(4000.0, 4002.5), ["scale_out"],
            starting_balance=10_000.0, spread_pts=0.0)

        assert stats["scale_out"].trades == 0


class TestThePickerOffersTicks:
    """Structural: the page builds its selector inside a NiceGUI context
    that cannot be entered without a running server."""

    def _code(self) -> str:
        src = (pathlib.Path(__file__).resolve().parents[2]
               / "frontend" / "pages" / "backtest.py").read_text(encoding="utf-8")
        return "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))

    def test_ticks_is_offered_as_a_granularity(self):
        assert "Ticks" in self._code()

    def test_it_calls_the_tick_bridge_method(self):
        assert "get_ticks_range" in self._code()
