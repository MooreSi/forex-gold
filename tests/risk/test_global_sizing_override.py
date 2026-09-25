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
    """Fixed lots is live exactly when a fixed lot is stored, as every order
    path has always read it. Risk % parks the lot in its own column, where it
    sizes nothing."""

    def test_a_stored_fixed_lot_is_fixed_lots_mode(self):
        assert ls.sizing_mode({"strategy_lot_size": 0.1}) == ls.MODE_LOTS
        assert ls.global_fixed_lot({"strategy_lot_size": 0.1}) == 0.1

    def test_no_fixed_lot_is_risk_mode(self):
        assert ls.sizing_mode({"strategy_lot_size": 0}) == ls.MODE_RISK
        assert ls.sizing_mode({}) == ls.MODE_RISK

    def test_a_parked_lot_sizes_nothing(self):
        rs = {"strategy_lot_size": 0, "strategy_lot_size_parked": 0.1}
        assert ls.sizing_mode(rs) == ls.MODE_RISK
        assert ls.global_fixed_lot(rs) == 0.0


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
        rs = {"strategy_lot_size": 0.77, "max_lot_size": 1.0}
        assert _lot(rs, _SINGLE).lot == 0.1


class TestOverrideOnUsesTheGlobalSize:
    def test_fixed_lots_replaces_the_anchor_lot(self):
        rs = {"global_sizing_override": 1, "strategy_lot_size": 0.03, "max_lot_size": 0.5}
        sized = _lot(rs, _SINGLE)
        assert sized.lot == 0.03
        assert sized.fixed is True

    def test_fixed_lots_is_per_leg_on_a_grid(self):
        rs = {"global_sizing_override": 1, "strategy_lot_size": 0.03, "max_lot_size": 0.5}
        assert _lot(rs, _GRID).lot == 0.03

    def test_risk_mode_replaces_the_template_and_its_own_percentage(self):
        rs = {"global_sizing_override": 1, "strategy_lot_size": 0,
              "strategy_lot_size_parked": 0.03, "risk_per_trade_pct": 2.0}
        sized = _lot(rs, dict(_SINGLE, risk_pct=0.5))
        assert sized.lot == 0.2           # the global 2%
        assert sized.fixed is False

    def test_risk_is_the_total_for_the_signal_split_across_grid_legs(self):
        # Owner, 2026-09-25: 2% means 2% of the account on the signal, whether
        # the template opens one leg or four.
        rs = {"global_sizing_override": 1, "strategy_lot_size": 0,
              "strategy_lot_size_parked": 0.03, "risk_per_trade_pct": 2.0}
        assert _lot(rs, _GRID).lot == 0.1          # 1% per leg, two legs
        assert _lot(rs, dict(_GRID, anchors=1, pendings=3)).lot == 0.05

    def test_a_single_mode_template_is_one_leg_whatever_its_pending_count(self):
        # Single mode opens one position; its pendings field does nothing.
        rs = {"global_sizing_override": 1, "strategy_lot_size": 0,
              "strategy_lot_size_parked": 0.03, "risk_per_trade_pct": 2.0}
        assert _lot(rs, dict(_SINGLE, pendings=3)).lot == 0.2

    def test_fixed_lots_is_capped_by_the_maximum(self):
        rs = {"global_sizing_override": 1, "strategy_lot_size": 0.3, "max_lot_size": 0.1}
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
    _CURRENT = {"strategy_lot_size": 0.1, "max_lot_size": 0.1}

    def test_a_maximum_below_the_minimum_lot_is_refused(self):
        with pytest.raises(ValueError, match="Maximum lot size"):
            ls.validate_update({"max_lot_size": 0}, self._CURRENT)

    def test_a_valid_maximum_is_accepted(self):
        ls.validate_update({"max_lot_size": 0.2}, self._CURRENT)

    def test_a_fixed_lot_below_the_broker_minimum_is_refused(self):
        with pytest.raises(ValueError, match="at least 0.01"):
            ls.validate_update({"strategy_lot_size": 0.005}, self._CURRENT)

    def test_zero_is_accepted_because_it_means_risk_mode(self):
        ls.validate_update({"strategy_lot_size": 0}, self._CURRENT)

    def test_a_fixed_lot_above_the_maximum_is_refused(self):
        with pytest.raises(ValueError, match="Maximum lot size"):
            ls.validate_update({"strategy_lot_size": 0.5}, self._CURRENT)

    def test_lowering_the_maximum_under_the_fixed_lot_is_refused(self):
        with pytest.raises(ValueError, match="Maximum lot size"):
            ls.validate_update({"max_lot_size": 0.05}, self._CURRENT)

    def test_an_unrelated_save_is_not_blocked_by_an_old_bad_value(self):
        # This install holds max_lot_size = 0 today. Ticking the give-back
        # guard must not be refused over a field the operator did not touch.
        ls.validate_update({"giveback_guard_enabled": 1},
                           dict(self._CURRENT, max_lot_size=0.0))

    def test_the_settings_service_refuses_before_writing(self, fresh_db):
        from backend.src.db import database as db
        from backend.src.services.risk import settings
        db.update_risk_settings({"max_lot_size": 0.1})
        with pytest.raises(ValueError):
            settings.update({"max_lot_size": 0})
        assert db.get_risk_settings()["max_lot_size"] == 0.1
