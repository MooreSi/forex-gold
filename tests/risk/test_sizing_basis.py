"""How a trade was sized, in words, for the trade-open alert (2026-09-28).

The owner asked that "if the trade is using % risk of capital to place the
trade state this in the telegram message". The trade row does not record how
its lot was chosen, so the alert describes it from the same settings
`template_lot` decides from -- through a function beside it, so the two read
the same rules.

Pure functions over dicts. Nothing here reaches a broker.
"""
from backend.src.services.risk import lot_sizing as ls

_SINGLE = {"mode": "single", "anchors": 1, "pendings": 0,
           "lot_anchor": 0.1, "risk_pct": 0.0}
_GRID = {"mode": "grid", "anchors": 1, "pendings": 3,
         "lot_anchor": 0.04, "risk_pct": 0.0}


class TestATemplateSizingItself:
    def test_its_fixed_lots_are_not_a_risk_percentage(self):
        assert ls.sizing_basis({}, _SINGLE) == ""

    def test_its_own_risk_pct_is_stated(self):
        assert ls.sizing_basis({}, {**_SINGLE, "risk_pct": 1.5}) == "1.5% of balance"

    def test_on_a_grid_its_risk_pct_is_per_leg(self):
        # template_lot hands the template's risk % to every leg unchanged.
        assert (ls.sizing_basis({}, {**_GRID, "risk_pct": 0.5})
                == "0.5% of balance per leg x 4")


class TestTheGlobalOverride:
    def test_global_risk_is_stated_as_the_signal_total(self):
        rs = {"global_sizing_override": 1, "strategy_lot_size": 0,
              "risk_per_trade_pct": 2.0}
        assert ls.sizing_basis(rs, _SINGLE) == "2% of balance"

    def test_global_risk_on_a_grid_names_the_split(self):
        rs = {"global_sizing_override": 1, "strategy_lot_size": 0,
              "risk_per_trade_pct": 2.0}
        assert (ls.sizing_basis(rs, _GRID)
                == "2% of balance (0.5% per leg x 4)")

    def test_global_fixed_lots_are_not_a_risk_percentage(self):
        rs = {"global_sizing_override": 1, "strategy_lot_size": 0.1,
              "risk_per_trade_pct": 2.0}
        assert ls.sizing_basis(rs, {**_SINGLE, "risk_pct": 1.5}) == ""


class TestNoTemplate:
    def test_risk_mode_states_the_global_percentage(self):
        rs = {"strategy_lot_size": 0, "risk_per_trade_pct": 0.75}
        assert ls.sizing_basis(rs, None) == "0.75% of balance"

    def test_fixed_lots_mode_states_nothing(self):
        assert ls.sizing_basis({"strategy_lot_size": 0.1}, None) == ""
