"""Profit Take works with Dynamic Position Management switched off.

Found during the 2026-09-01 demo session. The control lives under a "Dynamic
Position Management" heading and its tooltip reads "DPM will keep managing the
trade... 0 = DPM decides entirely", so the owner reasonably concluded demo 4
needed DPM on. It does not: `check_profit_close_target` is called from
`monitor_cycle` at indent 12, a sibling of the `if dpm_enabled` block, not
nested inside it. Turning DPM on to satisfy the label would have changed how
every open trade was managed in the middle of a money-path demo.

Two properties, and the first is the one that matters:

  * the behaviour -- the check must keep running with DPM off, because that is
    what the code does today and changing it would alter live trade management
  * the wording -- it must stop claiming otherwise
"""
from __future__ import annotations

from tests.frontend._source import module_source


class TestTheCheckDoesNotDependOnDpm:
    def test_the_profit_close_call_is_not_inside_the_dpm_block(self):
        """Indentation, read from the file, because this is precisely the
        thing that is easy to assert loosely and get wrong. `if dpm_enabled`
        sits at indent 12; the loop that calls the profit-close check sits at
        indent 12 too, so it is a sibling and not a child."""
        import pathlib

        from backend.src.services.positions import monitor_cycle

        lines = pathlib.Path(monitor_cycle.__file__).read_text(
            encoding="utf-8").split("\n")

        gate = next(i for i, l in enumerate(lines)
                    if 'if bool(rs.get("dpm_enabled", 0))' in l)
        call = next(i for i, l in enumerate(lines)
                    if "_check_profit_close_target_impl(" in l and "import" not in l)
        loop = max(i for i, l in enumerate(lines[:call])
                   if l.strip().startswith("for trade in open_trades:"))

        gate_indent = len(lines[gate]) - len(lines[gate].lstrip())
        loop_indent = len(lines[loop]) - len(lines[loop].lstrip())

        assert loop_indent <= gate_indent, (
            "the profit-close check has moved inside the DPM block; it now "
            "silently does nothing for anyone with DPM off"
        )

    def test_the_check_itself_only_looks_at_the_threshold(self):
        """It gates on profit_close_usd and on a real entry price. Not on
        dpm_enabled."""
        import pathlib

        from backend.src.services.positions import monitor_loop

        src = pathlib.Path(monitor_loop.__file__).read_text(encoding="utf-8")
        body = src[src.index("async def check_profit_close_target"):
                   src.index("async def", src.index("async def check_profit_close_target") + 10)]

        assert "dpm_enabled" not in body


class TestTheWordingDoesNotClaimOtherwise:
    def test_the_tooltip_no_longer_says_dpm_decides(self):
        src = module_source("frontend/pages/settings.py")

        assert "0 = DPM decides entirely" not in src

    def test_it_says_the_setting_works_regardless(self):
        """The positive half. Removing the misleading sentence without saying
        what is true leaves the heading doing the misleading on its own."""
        src = module_source("frontend/pages/settings.py")
        card = src[src.index("Profit Take ($)"):]
        card = card[:card.index("def ") if "def " in card else len(card)]

        assert "whether or not" in card.lower() or "even with" in card.lower()

    def test_the_placeholder_does_not_say_dpm_decides(self):
        src = module_source("frontend/pages/settings.py")

        assert "0 = DPM decides" not in src
