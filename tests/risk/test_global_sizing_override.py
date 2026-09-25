"""One global per-trade size, and the switch that makes EA templates obey it.

docs/todo/risk/010. Trading > Risk > Per trade holds EITHER a risk % OR a
fixed lot size, never both; the "EA template override" switch makes every
automated trade use it instead of the template's own lots and risk %.

The incident behind it: on 2026-09-24 Gold Diggers VIP sent MT5 an order for
0 lots twice (`EA rejected template order: invalid volume`), because the
template's 0.1 anchor lot was capped by a Maximum lot size of 0.

Pure functions over dicts. Nothing here reaches a broker.
"""
import pytest

from backend.src.services.risk import lot_sizing as ls

_SINGLE = {"name": "Single", "mode": "single", "anchors": 1, "pendings": 0,
           "lot_anchor": 0.1, "lot_pending": 0.0, "risk_pct": 0.0}
_GRID = {"name": "Grid", "mode": "grid", "anchors": 1, "pendings": 1,
         "lot_anchor": 0.04, "lot_pending": 0.04, "risk_pct": 0.0}


def _suggest(entry, stop_loss, balance, risk_pct):
    """Stands in for suggest_lot_size: hands back the % it was asked for, so
    a test can see WHICH percentage was used and how it was divided."""
    return round(risk_pct / 10.0, 4)


def _lot(rs, template, *, balance=1000.0):
    return ls.template_lot(rs, template, 4270.0, 4263.0, balance, _suggest)


class TestTheModeIsOneOrTheOther:
    def test_risk_mode_hides_a_stored_fixed_lot(self):
        assert ls.global_fixed_lot({"global_sizing_mode": "risk",
                                    "strategy_lot_size": 0.1}) == 0.0

    def test_lots_mode_returns_it(self):
        assert ls.global_fixed_lot({"global_sizing_mode": "lots",
                                    "strategy_lot_size": 0.1}) == 0.1

    def test_a_row_without_the_column_keeps_todays_precedence(self):
        # Before this change a fixed lot above 0 always beat Risk per trade %.
        # A client that has not run the migration must size exactly as before.
        assert ls.sizing_mode({"strategy_lot_size": 0.1}) == ls.MODE_LOTS
        assert ls.sizing_mode({"strategy_lot_size": 0}) == ls.MODE_RISK
        assert ls.sizing_mode({}) == ls.MODE_RISK

    def test_the_column_wins_over_the_derivation(self):
        assert ls.sizing_mode({"global_sizing_mode": "risk",
                               "strategy_lot_size": 0.1}) == ls.MODE_RISK

    def test_an_unknown_mode_falls_back_to_the_derivation(self):
        assert ls.sizing_mode({"global_sizing_mode": "bogus",
                               "strategy_lot_size": 0.2}) == ls.MODE_LOTS


class TestOverrideOffLeavesTemplatesAlone:
    def test_a_fixed_lot_template_uses_its_own_anchor_lot(self):
        sized = _lot({"max_lot_size": 0.5, "risk_per_trade_pct": 2.0}, _SINGLE)
        assert sized.lot == 0.1
        assert sized.fixed is True

    def test_its_anchor_lot_is_still_capped_by_the_maximum(self):
        assert _lot({"max_lot_size": 0.05}, _SINGLE).lot == 0.05

    def test_a_risk_template_uses_its_own_percentage(self):
        sized = _lot({"risk_per_trade_pct": 2.0},
                     dict(_SINGLE, risk_pct=0.5))
        assert sized.lot == 0.05          # 0.5%, not the global 2%
        assert sized.fixed is False

    def test_the_global_fixed_lot_does_not_reach_a_template(self):
        rs = {"global_sizing_mode": "lots", "strategy_lot_size": 0.77,
              "max_lot_size": 1.0}
        assert _lot(rs, _SINGLE).lot == 0.1


class TestOverrideOnUsesTheGlobalSize:
    def test_fixed_lots_replaces_the_anchor_lot(self):
        rs = {"global_sizing_override": 1, "global_sizing_mode": "lots",
              "strategy_lot_size": 0.03, "max_lot_size": 0.5}
        sized = _lot(rs, _SINGLE)
        assert sized.lot == 0.03
        assert sized.fixed is True

    def test_fixed_lots_is_per_leg_on_a_grid(self):
        rs = {"global_sizing_override": 1, "global_sizing_mode": "lots",
              "strategy_lot_size": 0.03, "max_lot_size": 0.5}
        assert _lot(rs, _GRID).lot == 0.03

    def test_risk_mode_replaces_the_template_and_its_own_percentage(self):
        rs = {"global_sizing_override": 1, "global_sizing_mode": "risk",
              "risk_per_trade_pct": 2.0}
        sized = _lot(rs, dict(_SINGLE, risk_pct=0.5))
        assert sized.lot == 0.2           # the global 2%
        assert sized.fixed is False

    def test_risk_is_the_total_for_the_signal_split_across_grid_legs(self):
        # Owner, 2026-09-25: 2% means 2% of the account on the signal, whether
        # the template opens one leg or four.
        rs = {"global_sizing_override": 1, "global_sizing_mode": "risk",
              "risk_per_trade_pct": 2.0}
        assert _lot(rs, _GRID).lot == 0.1          # 1% per leg, two legs
        assert _lot(rs, dict(_GRID, anchors=1, pendings=3)).lot == 0.05

    def test_a_single_mode_template_is_one_leg_whatever_its_pending_count(self):
        # Single mode opens one position; its pendings field does nothing.
        rs = {"global_sizing_override": 1, "global_sizing_mode": "risk",
              "risk_per_trade_pct": 2.0}
        assert _lot(rs, dict(_SINGLE, pendings=3)).lot == 0.2

    def test_fixed_lots_is_capped_by_the_maximum(self):
        rs = {"global_sizing_override": 1, "global_sizing_mode": "lots",
              "strategy_lot_size": 0.3, "max_lot_size": 0.1}
        assert _lot(rs, _SINGLE).lot == 0.1


class TestTheTemplatesOwnLegLots:
    """The EA stages grid legs at the template's lot_anchor/lot_pending and
    only falls back to the lot Python sends when those are 0. Whenever the
    template is NOT the source of the size, the copy sent to the EA must
    carry the computed lot or the override would do nothing on a grid."""

    def test_override_off_fixed_lot_template_keeps_its_leg_lots(self):
        assert ls.template_sizes_its_own_legs({}, _GRID) is True

    def test_override_on_does_not(self):
        assert ls.template_sizes_its_own_legs(
            {"global_sizing_override": 1}, _GRID) is False

    def test_a_risk_template_does_not_either(self):
        assert ls.template_sizes_its_own_legs({}, dict(_GRID, risk_pct=1.0)) is False


class TestAZeroLotNeverReachesTheBroker:
    def test_zero_is_refused_naming_the_maximum_when_that_is_the_cause(self):
        with pytest.raises(ls.UnplaceableLot) as exc:
            ls.refuse_unplaceable(0.0, {"max_lot_size": 0.0})
        assert "Maximum lot size" in str(exc.value)

    def test_zero_is_refused_even_when_the_maximum_is_fine(self):
        with pytest.raises(ls.UnplaceableLot):
            ls.refuse_unplaceable(0.0, {"max_lot_size": 0.1})

    def test_the_minimum_lot_is_placeable(self):
        ls.refuse_unplaceable(0.01, {"max_lot_size": 0.1})

    def test_the_incident_reproduces_and_is_refused(self):
        # 2026-09-24: template anchor 0.1, Maximum lot size 0, override off.
        sized = _lot({"max_lot_size": 0.0, "risk_per_trade_pct": 2.0}, _SINGLE)
        assert sized.lot == 0.0
        with pytest.raises(ls.UnplaceableLot):
            ls.refuse_unplaceable(sized.lot, {"max_lot_size": 0.0})


class TestTheSettingsCannotBeSavedBroken:
    _CURRENT = {"global_sizing_mode": "risk", "strategy_lot_size": 0.1,
                "max_lot_size": 0.1}

    def test_a_maximum_below_the_minimum_lot_is_refused(self):
        with pytest.raises(ValueError, match="Maximum lot size"):
            ls.validate_update({"max_lot_size": 0}, self._CURRENT)

    def test_a_valid_maximum_is_accepted(self):
        ls.validate_update({"max_lot_size": 0.01}, self._CURRENT)

    def test_lots_mode_with_no_lot_is_refused(self):
        with pytest.raises(ValueError, match="fixed lot"):
            ls.validate_update({"global_sizing_mode": "lots"},
                               dict(self._CURRENT, strategy_lot_size=0))

    def test_lots_mode_with_a_lot_is_accepted(self):
        ls.validate_update({"global_sizing_mode": "lots"}, self._CURRENT)

    def test_a_fixed_lot_above_the_maximum_is_refused_in_lots_mode(self):
        with pytest.raises(ValueError, match="Maximum lot size"):
            ls.validate_update({"strategy_lot_size": 0.5},
                               dict(self._CURRENT, global_sizing_mode="lots"))

    def test_an_unknown_mode_is_refused(self):
        with pytest.raises(ValueError):
            ls.validate_update({"global_sizing_mode": "both"}, self._CURRENT)

    def test_an_unrelated_save_is_not_blocked_by_an_old_bad_value(self):
        # This install holds max_lot_size = 0 today. Ticking the give-back
        # guard must not be refused over a field the operator did not touch.
        ls.validate_update({"giveback_guard_enabled": 1},
                           dict(self._CURRENT, max_lot_size=0.0))
