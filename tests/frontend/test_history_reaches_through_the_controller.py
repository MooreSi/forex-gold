"""The History page asks a controller for its fee figures, not the runtime.

Three pages imported `_apply_fee` and `_platform_fee_rate` straight off
`backend.src.runtime`, which re-exports them from `services/broker/
mt5_performance`. Two layers skipped, and both names private -- the page was
reaching past the boundary for something with a leading underscore on it.

`services/broker/fees.py` and `history_controller.platform_fee_rate` already
existed for exactly this and two of the three call sites already used them.
These pin the last of it, and pin that the numbers did not move.
"""
from __future__ import annotations

import pytest

from tests.frontend._source import module_source

HISTORY_MODULES = (
    "frontend/pages/history/__init__.py",
    "frontend/pages/history/_calendar.py",
    "frontend/pages/history/_trade_table.py",
)


class TestTheBoundary:
    @pytest.mark.parametrize("rel", HISTORY_MODULES)
    def test_no_history_module_imports_the_runtime(self, rel):
        assert "backend.src.runtime" not in module_source(rel)

    def test_the_check_can_see_an_import(self):
        """Negative control: the assertion above is a substring test, and a
        substring test that matches nothing proves nothing.

        The sample line is assembled rather than written out, because
        tests/refactor/test_runtime_has_no_dead_imports.py scans this whole
        repository for `from backend.src.runtime import ...` to decide which
        of runtime's imports are load-bearing re-exports. A literal one here
        would tell it a page still needs a name that no page imports.
        """
        sample = "from " + "backend.src.runtime" + " import _apply_fee"

        assert "backend.src.runtime" in sample

    @pytest.mark.parametrize("rel", HISTORY_MODULES)
    def test_no_history_module_reaches_the_performance_service(self, rel):
        """Going straight to mt5_performance instead would satisfy the test
        above while skipping the same two layers.

        Import lines only. `compute_mt5_performance` is a method the page
        legitimately calls on the engine, and a bare substring test for
        "mt5_performance" reads that as a violation.
        """
        for line in module_source(rel).splitlines():
            stripped = line.strip()
            if not stripped.startswith(("import ", "from ")):
                continue
            assert "mt5_performance" not in stripped, stripped
            assert "services.broker" not in stripped, stripped


class TestTheControllerOffersIt:
    def test_apply_fee_is_on_the_controller(self):
        from backend.src.controllers import history_controller as ctl

        assert callable(ctl.apply_fee)

    def test_it_returns_what_the_service_returns(self):
        """Same inputs, same tuple. A forwarder that reshapes the answer is
        the failure this whole layering exists to prevent."""
        from backend.src.controllers import history_controller as ctl
        from backend.src.services.broker import fees as _fees

        deals = [{"profit": 100.0, "swap": -1.5, "fee": -7.0}]

        assert ctl.apply_fee(deals, 1.0, 8.0) == _fees.apply_fee(deals, 1.0, 8.0)

    def test_the_live_ecn_branch_is_the_one_being_compared(self):
        """Negative control for the test above: with a zero fee field the
        function takes its other branch, so if both tests returned the same
        thing the comparison would be covering only half the function."""
        from backend.src.controllers import history_controller as ctl

        charged = ctl.apply_fee([{"profit": 100.0, "swap": 0.0, "fee": -7.0}], 1.0, 8.0)
        estimated = ctl.apply_fee([{"profit": 100.0, "swap": 0.0, "fee": 0.0}], 1.0, 8.0)

        assert charged != estimated

    def test_the_numbers_it_produces_have_not_moved(self):
        """Characterization. These are the figures on the Closed Trades table,
        the equity curve and the calendar heatmap -- the ones the owner reads
        to judge whether the app is working. Pinned to the digit.

        Live ECN: the fee is already inside the deals' own profit/swap/fee, so
        the P&L is their sum and the fee reported is its magnitude.
        Demo: the fee field is zero, so open_lots x rate is estimated and
        deducted, otherwise a demo account looks more profitable than a live
        one running the identical strategy.
        """
        from backend.src.controllers import history_controller as ctl

        live = [{"profit": 250.0, "swap": -3.25, "fee": -14.0}]
        assert ctl.apply_fee(live, 2.0, 7.0) == (232.75, 14.0)

        # A different rate on purpose: at 7.0 the demo branch lands on the
        # same 232.75 as the live one by arithmetic accident, and two branches
        # pinned to one number cannot tell you they were swapped.
        demo = [{"profit": 250.0, "swap": -3.25, "fee": 0.0}]
        assert ctl.apply_fee(demo, 2.0, 5.0) == (236.75, 10.0)

    def test_no_fee_is_charged_when_there_is_no_rate(self):
        from backend.src.controllers import history_controller as ctl

        assert ctl.apply_fee([{"profit": 10.0, "swap": 0.0, "fee": 0.0}], 2.0, 0.0) == (10.0, 0.0)


class TestTheRuntimeStopsCarryingThem:
    def test_the_runtime_no_longer_re_exports_the_fee_helpers(self):
        """The frontend import was kept alive only to stop runtime's own
        import reading as dead (see the comment it replaced). With the page
        rewired, the re-export has no caller and goes too -- otherwise the
        dead-import gate is satisfied by a chain that exists to satisfy it."""
        import pathlib

        import backend.src.runtime as rt

        src = pathlib.Path(rt.__file__).read_text(encoding="utf-8")
        assert "_apply_fee" not in src
        assert "_platform_fee_rate" not in src
