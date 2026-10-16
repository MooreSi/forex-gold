"""Round-number exits for EA Template levels (docs/todo/019 A).

Pure rules plus the two places they are applied, `template_sl_at` and
`resolve_template_tps`. Nothing here reaches a broker: no bridge, no order
call, only price arithmetic on dicts. Pinned: with the setting off both
functions return exactly what they did before; a stop only ever moves
further from entry; a target is never pulled past the entry; a ladder taken
from a Telegram message is left alone.
"""
import pytest

from backend.src.db import database as db
from backend.src.services.positions.core_pips import PIPS_TO_PRICE_XAUUSD
from backend.src.services.trading import round_levels as rl
from backend.src.services.trading import template_levels as tl
from backend.src.services.trading.open_trade import resolve_template_tps


@pytest.fixture
def setting(monkeypatch):
    state = {"round_number_exits_enabled": 0}
    monkeypatch.setattr(db, "get_risk_settings", lambda: dict(state))
    return state


class TestTargets:
    @pytest.mark.parametrize("tp,want", [
        (2000.00, 1999.70),     # on the level
        (2000.60, 1999.70),     # just past it
        (2001.00, 1999.70),     # exactly ZONE past it
        (2001.01, 2001.01),     # outside the zone: unchanged
        (2003.50, 2003.50),
        (1999.50, 1999.50),     # short of the level already
    ])
    def test_buy(self, tp, want):
        assert rl.adjust_tp(tp, entry=1990.0, is_buy=True) == pytest.approx(want)

    @pytest.mark.parametrize("tp,want", [
        (2000.00, 2000.30),
        (1999.40, 2000.30),
        (1999.00, 2000.30),
        (1998.99, 1998.99),
        (2000.50, 2000.50),
    ])
    def test_sell(self, tp, want):
        assert rl.adjust_tp(tp, entry=2010.0, is_buy=False) == pytest.approx(want)

    def test_a_target_that_would_cross_the_entry_is_left_alone(self):
        assert rl.adjust_tp(2000.40, entry=1999.80, is_buy=True) == 2000.40
        assert rl.adjust_tp(1999.60, entry=2000.20, is_buy=False) == 1999.60


class TestStops:
    @pytest.mark.parametrize("sl,want", [
        (2000.00, 1998.70),     # on the level
        (1999.40, 1998.70),     # just below it
        (1999.00, 1998.70),     # exactly ZONE below
        (1998.99, 1998.99),     # outside: unchanged
        (1997.00, 1997.00),
    ])
    def test_buy(self, sl, want):
        assert rl.adjust_sl(sl, entry=2010.0, is_buy=True) == pytest.approx(want)

    @pytest.mark.parametrize("sl,want", [
        (2000.00, 2001.30),
        (2000.60, 2001.30),
        (2001.00, 2001.30),
        (2001.01, 2001.01),
    ])
    def test_sell(self, sl, want):
        assert rl.adjust_sl(sl, entry=1990.0, is_buy=False) == pytest.approx(want)

    @pytest.mark.parametrize("sl,entry,buy", [
        (s / 100, 2010.0, True) for s in range(199500, 200600, 7)
    ] + [(s / 100, 1990.0, False) for s in range(199500, 200600, 7)])
    def test_a_stop_never_moves_closer_to_entry(self, sl, entry, buy):
        new = rl.adjust_sl(sl, entry=entry, is_buy=buy)
        assert abs(entry - new) >= abs(entry - sl) - 1e-9


class TestTemplateStop:
    TPL = {"sl_pips": 100}

    def test_off_is_unchanged(self, setting):
        # 2010.00 - 100 pips = 2000.00, on a round level.
        assert tl.template_sl_at(self.TPL, "BUY", 2010.0) == round(
            2010.0 - 100 * PIPS_TO_PRICE_XAUUSD, 2)

    def test_on_moves_the_stop_off_the_level(self, setting):
        setting["round_number_exits_enabled"] = 1
        assert tl.template_sl_at(self.TPL, "BUY", 2010.0) == pytest.approx(1998.70)
        assert tl.template_sl_at(self.TPL, "SELL", 1990.0) == pytest.approx(2001.30)

    def test_on_with_no_template_stop_still_returns_none(self, setting):
        setting["round_number_exits_enabled"] = 1
        assert tl.template_sl_at({"sl_pips": 0}, "BUY", 2010.0) is None

    def test_an_unreadable_setting_is_off(self, monkeypatch):
        def boom():
            raise RuntimeError("db gone")
        monkeypatch.setattr(db, "get_risk_settings", boom)
        assert tl.template_sl_at(self.TPL, "BUY", 2010.0) == 2000.0


class TestTemplateLadder:
    # ask 1995.00: tp1 50 pips -> 2000.00, tp2 125 pips -> 2007.50
    TPL = {"tp1_pips": 50, "tp1_pct": 50, "tp2_pips": 125, "tp2_pct": 50}
    TICK = tl.PriceRef(1995.0)

    def _tps(self, template=None, signal_tps=(), source=None):
        tps, _, _ = resolve_template_tps(template or self.TPL, "BUY", self.TICK,
                                         list(signal_tps), source)
        return tps

    def test_off_is_unchanged(self, setting):
        assert self._tps() == {1: pytest.approx(1995.0 + 50 * PIPS_TO_PRICE_XAUUSD),
                               2: pytest.approx(1995.0 + 125 * PIPS_TO_PRICE_XAUUSD)}

    def test_on_pulls_a_target_before_the_level_and_leaves_the_rest(self, setting):
        setting["round_number_exits_enabled"] = 1
        assert self._tps() == {1: pytest.approx(1999.70), 2: pytest.approx(2007.50)}

    def test_on_leaves_a_telegram_anchored_ladder_alone(self, setting, monkeypatch):
        setting["round_number_exits_enabled"] = 1
        import backend.src.services.trading.open_trade as ot
        monkeypatch.setattr(ot, "is_telegram_source", lambda s: True)
        tpl = dict(self.TPL, tp_from_telegram=True)
        assert self._tps(tpl, signal_tps=[2000.0, 2010.0], source="Some Channel") == {
            1: 2000.0, 2: 2010.0}
