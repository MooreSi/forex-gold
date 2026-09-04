"""A refused template must say it was refused, not report a row of zeros.

From the owner's question on 2026-09-04, looking at a Strategy Comparison
table: "why does the top strategy show all 0?"

    30 TP1 SL50 and Trail   0   0   0   0%   +$0.00   ...   Prof Factor inf   $1,000.00
    Asian - Single         90  47  43  52%  +$512.00  ...
    GD VIP - Single        90  68  22  76%  +$121.00  ...

Nothing was wrong with the template or the signals. The run was tick-based,
and `30 TP1 SL50 and Trail` uses trail_mode=candle, which the TICK walk
deliberately refuses -- the EA trails to the last 3 closed M15 candles and the
tick walk has bid/ask, not a candle series. The bar walk supports it. So the
same template is simulated on bars and refused on ticks, and the refusal
arrived as `None` per signal, which aggregated into zeros.

Returning None rather than approximating is right and must not change: "a
plausible number from a template the walk cannot model is worse than no
number, because it would be used to choose what trades real money". But zeros
in a comparison table do not read as "refused". They read as a strategy that
traded nothing and lost nothing -- which, sitting beside a row showing 62.8%
max drawdown, is an argument FOR the refused template. The reason has to
travel with the result.

template_support.summarise already takes this position for the picker: "Every
template gets a row, including the unsupported ones. Omitting them would read
as 'this template does not exist' rather than 'this template cannot be
backtested, and here is why'."
"""
from __future__ import annotations

import pytest

from backend.src.services.backtest import engine as bt


def _sig(**over):
    base = dict(
        signal_id="s1", direction="BUY", entry_low=3999.5, entry_high=4000.5,
        stop_loss=3995.0, tp1=4002.0, tp2=None, tp3=None, created_ts=0.0,
    )
    base.update(over)
    return bt.BtSignal(**base)


_TS0 = 20_000.0


def _ticks(*bids, spread=0.02) -> list[dict]:
    return [{"time": _TS0 + i, "bid": b, "ask": b + spread}
            for i, b in enumerate(bids)]


def _bars(*closes):
    return [{"ts": _TS0 + i * 60, "open": c, "high": c + 1.0,
             "low": c - 1.0, "close": c}
            for i, c in enumerate(closes)]


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


def test_a_candle_trail_on_ticks_reports_why_it_was_refused(templates):
    """The exact case from the table. Zeros alone are indistinguishable from
    a strategy that simply never triggered."""
    templates["Sim Me"] = _tpl(trail_mode="candle", trail_distance=40.0)

    stats = bt.run_backtest_ticks(
        [_sig()], _ticks(4000.0, 4002.5), ["template:Sim Me"],
        starting_balance=10_000.0, spread_pts=0.0)

    row = stats["template:Sim Me"]
    assert row.trades == 0
    assert row.unsupported_reason
    assert "candle" in row.unsupported_reason


def test_the_same_template_is_supported_on_bars(templates):
    """The half that makes the tick refusal confusing: it is not a broken
    template, and a user who tests it on bars sees it work."""
    templates["Sim Me"] = _tpl(trail_mode="candle", trail_distance=40.0)

    stats = bt.run_backtest(
        [_sig()], _bars(4000.0, 4001.0, 4002.5), ["template:Sim Me"],
        starting_balance=10_000.0, spread_pts=0.0)

    assert stats["template:Sim Me"].unsupported_reason == ""


def test_a_supported_template_carries_no_reason(templates):
    stats = bt.run_backtest_ticks(
        [_sig()], _ticks(4000.0, 4002.5), ["template:Sim Me"],
        starting_balance=10_000.0, spread_pts=0.0)

    row = stats["template:Sim Me"]
    assert row.trades == 1
    assert row.unsupported_reason == ""


def test_a_grid_template_says_so_on_either_walk(templates):
    """Grid is refused by can_simulate itself rather than by the trail check,
    so it exercises the other half of the guard."""
    templates["Sim Me"] = _tpl(mode="grid")

    ticks = bt.run_backtest_ticks(
        [_sig()], _ticks(4000.0, 4002.5), ["template:Sim Me"],
        starting_balance=10_000.0, spread_pts=0.0)
    bars = bt.run_backtest(
        [_sig()], _bars(4000.0, 4001.0, 4002.5), ["template:Sim Me"],
        starting_balance=10_000.0, spread_pts=0.0)

    assert ticks["template:Sim Me"].unsupported_reason
    assert bars["template:Sim Me"].unsupported_reason


def test_a_missing_template_is_not_reported_as_unsupported(templates):
    """"This template no longer exists" is a different problem from "this
    template cannot be walked", and conflating them sends the user to edit a
    trail mode on a template that is not there."""
    stats = bt.run_backtest_ticks(
        [_sig()], _ticks(4000.0, 4002.5), ["template:No Such Template"],
        starting_balance=10_000.0, spread_pts=0.0)

    assert stats["template:No Such Template"].unsupported_reason == ""


def test_a_non_template_strategy_carries_no_reason(templates):
    """Built-in strategies are not walked on ticks at all. That is a
    different silence, and mislabelling it 'unsupported template' would be
    wrong."""
    stats = bt.run_backtest_ticks(
        [_sig()], _ticks(4000.0, 4002.5), ["scale_out"],
        starting_balance=10_000.0, spread_pts=0.0)

    assert stats["scale_out"].unsupported_reason == ""
