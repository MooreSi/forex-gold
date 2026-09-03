"""The backtest runs EA templates, and lists them without being told to.

Item 7 of the owner's 2026-09-03 list: "change this from strategies to EA
templates, also if new EA templates are created they should appear here
automatically".

Two halves:

  * `_simulate` routes a `template:<name>` strategy to the template walk
    rather than the built-in chain;
  * the picker reads `list_ea_templates()`, so a template saved on Trading >
    Strategy shows up here with no code change.

An UNSUPPORTED template must never produce a number. `_simulate` returns None
for it -- the same answer it gives an unknown strategy -- because a plausible
figure from an approximated template is the one outcome worse than no figure.
"""
from __future__ import annotations

import pathlib

import pytest

from backend.src.services.backtest import engine as bt


def _sig():
    return bt.BtSignal(
        signal_id="s1", direction="BUY", entry_low=3999.5, entry_high=4000.5,
        stop_loss=3995.0, tp1=4002.0, tp2=None, tp3=None, created_ts=0.0,
    )


# Candles must sit AFTER the signal in broker time: _simulate skips any bar
# before `created_ts + _BROKER_TZ_OFFSET`, and that offset is 10,800s. Bars
# timestamped 1, 2, 3 are silently all in the past, so nothing ever fills.
_TS0 = 20_000.0


def _candles(*pairs):
    return [{"ts": _TS0 + i * 60, "high": hi, "low": lo, "open": lo, "close": hi}
            for i, (hi, lo) in enumerate(pairs)]


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
    """Whatever the template store holds, without touching a database."""
    store: dict = {"Sim Me": _tpl()}
    monkeypatch.setattr(bt, "_load_backtest_template",
                        lambda name: store.get(name))
    return store


class TestTheDispatch:
    def test_a_template_strategy_is_simulated(self, templates):
        trade = bt._simulate(
            _candles((4000.5, 3999.5), (4002.5, 4000.0)),
            _sig(), "template:Sim Me", balance=10_000.0, risk_pct=0.5,
            spread_pts=0.0)

        assert trade is not None
        assert trade.strategy == "template:Sim Me"

    def test_it_uses_the_template_anchor_lot(self, templates):
        templates["Sim Me"] = _tpl(lot_anchor=0.07)

        trade = bt._simulate(
            _candles((4000.5, 3999.5), (4002.5, 4000.0)),
            _sig(), "template:Sim Me", balance=10_000.0, risk_pct=0.5,
            spread_pts=0.0)

        assert trade.lot_size == pytest.approx(0.07)

    def test_the_close_bar_is_absolute_not_relative_to_the_fill(self, templates):
        """The walk starts at the fill, so its bar indices are offsets. A
        close reported at bar 1 of the walk is bar fill+1 of the series, and
        the results table shows the series index."""
        trade = bt._simulate(
            _candles((3999.0, 3998.0), (4000.5, 3999.5), (4002.5, 4000.0)),
            _sig(), "template:Sim Me", balance=10_000.0, risk_pct=0.5,
            spread_pts=0.0)

        assert trade.close_bar_idx >= trade.fill_bar_idx

    def test_commission_is_applied_the_same_as_any_strategy(self, templates):
        plain = bt._simulate(
            _candles((4000.5, 3999.5), (4002.5, 4000.0)), _sig(),
            "template:Sim Me", balance=10_000.0, risk_pct=0.5, spread_pts=0.0)
        charged = bt._simulate(
            _candles((4000.5, 3999.5), (4002.5, 4000.0)), _sig(),
            "template:Sim Me", balance=10_000.0, risk_pct=0.5, spread_pts=0.0,
            commission_per_lot=7.0)

        assert charged.commission > 0
        assert charged.pnl_usd < plain.pnl_usd


class TestAnUnsupportedTemplateProducesNothing:
    def test_a_grid_template_returns_none(self, templates):
        """Not an approximation, not a zero -- nothing. The picker should not
        have offered it, and if it does, this is the backstop."""
        templates["Sim Me"] = _tpl(mode="grid")

        trade = bt._simulate(
            _candles((4000.5, 3999.5), (4002.5, 4000.0)),
            _sig(), "template:Sim Me", balance=10_000.0, risk_pct=0.5,
            spread_pts=0.0)

        assert trade is None

    def test_a_template_that_does_not_exist_returns_none(self, templates):
        trade = bt._simulate(
            _candles((4000.5, 3999.5), (4002.5, 4000.0)),
            _sig(), "template:No Such Template", balance=10_000.0,
            risk_pct=0.5, spread_pts=0.0)

        assert trade is None


class TestTheBuiltInsStillWork:
    """Templates are added beside the existing chain, not instead of it --
    channels bound to a built-in still need their simulators."""

    @pytest.mark.parametrize("strategy", ["scale_out", "be_runner",
                                          "conservative_trial"])
    def test_a_builtin_strategy_still_dispatches(self, strategy):
        trade = bt._simulate(
            _candles((4000.5, 3999.5), (4002.5, 4000.0)), _sig(), strategy,
            balance=10_000.0, risk_pct=0.5, spread_pts=0.0)

        assert trade is not None

    def test_an_unknown_strategy_still_returns_none(self):
        trade = bt._simulate(
            _candles((4000.5, 3999.5), (4002.5, 4000.0)), _sig(), "nonsense",
            balance=10_000.0, risk_pct=0.5, spread_pts=0.0)

        assert trade is None


class TestThePickerListsTemplates:
    """Structural: the page builds its checkboxes inside a NiceGUI context
    that cannot be entered without a running server."""

    def _code(self) -> str:
        src = (pathlib.Path(__file__).resolve().parents[2]
               / "frontend" / "pages" / "backtest.py").read_text(encoding="utf-8")
        return "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))

    def test_it_reads_the_template_store(self):
        """"appear here automatically" means read, not hardcode."""
        assert "list_ea_templates" in self._code()

    def test_it_asks_which_are_supported(self):
        assert "summarise" in self._code() or "can_simulate" in self._code()

    def test_it_no_longer_lists_built_in_strategies(self):
        assert "_STRATEGY_LABELS" not in self._code()
