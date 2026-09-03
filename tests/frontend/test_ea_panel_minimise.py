"""The EA's on-chart panel can be collapsed so the candles are visible.

Owner, 2026-09-03: "on the ea i need an option to minimise the copier panel so
i can see the candles".

The EA is MQL5 and cannot be compiled or run from this repo -- it is built in
MetaEditor on the Windows side. So this pins the SOURCE: that the pieces exist
and are wired to each other. It cannot prove the panel draws correctly; only
loading the recompiled EA on a chart can do that.

What it can prove, and what has actually gone wrong in this file before, is
that a feature is half-wired -- a handler with no button, a state global
nothing reads, a load with no save.
"""
from __future__ import annotations

import pathlib

import pytest

EA = (pathlib.Path(__file__).resolve().parents[2]
      / "mql5" / "ForexTraderBridge.mq5")


def _code() -> str:
    """Source with // comments stripped.

    The comments here explain the minimise feature at length, so a plain
    substring search would match the explanation and pass with the code
    deleted. That trap has produced three false passes in this repo already.
    """
    out = []
    for ln in EA.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = ln.strip()
        if stripped.startswith("//"):
            continue
        out.append(ln.split("//")[0] if "//" in ln else ln)
    return "\n".join(out)


class TestTheToggleIsWiredEndToEnd:
    def test_the_state_global_exists(self):
        assert "g_panelMin " in _code() or "g_panelMin=" in _code()

    def test_there_is_a_button_to_click(self):
        assert 'PnlButton("min"' in _code()

    def test_the_click_is_handled(self):
        code = _code()
        assert 'what == "min"' in code

    def test_the_click_flips_the_state(self):
        assert "g_panelMin = !g_panelMin" in _code()

    def test_the_paint_branches_on_it(self):
        """Without this the state flips and the panel never changes."""
        assert "if(g_panelMin)" in _code()


class TestItSurvivesARecompile:
    """The EA is recompiled often. Re-opening a panel the user deliberately
    collapsed, on every recompile, would be its own annoyance."""

    def test_the_state_is_saved(self):
        assert "PanelMinSave" in _code()

    def test_it_is_loaded_on_init(self):
        code = _code()
        init = code[code.index("int OnInit()"):]
        init = init[:init.index("return(INIT_SUCCEEDED)")]

        assert "PanelMinLoad()" in init

    def test_it_is_keyed_per_chart(self):
        """Two charts each running the EA must not share one collapsed flag."""
        assert "ChartID()" in _code()


class TestTheCollapsedStripDoesNotChurn:
    def test_the_full_clear_is_guarded(self):
        """A delete-and-recreate on every timer repaint flickers the strip and
        churns the chart's object list. The guard is the whole reason
        g_panelMinDrawn exists."""
        code = _code()

        assert "g_panelMinDrawn" in code
        assert "if(!g_panelMinDrawn)" in code

    def test_expanding_resets_the_guard(self):
        """Left set, the next collapse would never clear the full panel."""
        code = _code()
        click = code[code.index('what == "min"'):]
        click = click[:click.index("return;")]

        assert "g_panelMinDrawn = false" in click


class TestNothingElseMoved:
    @pytest.mark.parametrize("name", [
        "refresh", "sound", "manual", "tabtrades",
    ])
    def test_the_other_header_buttons_survive(self, name):
        assert f'PnlButton("{name}"' in _code()

    def test_the_harvest_check_is_untouched(self):
        """Nothing about this change may touch what closes a position."""
        code = _code()

        assert "void CheckGlobalHarvest()" in code
        assert "if(g_globalHarvestEnabled) CheckGlobalHarvest();" in code


class TestGlobalHarvestIsVisible:
    """Owner, 2026-09-03: "global harvest doesn't appear to be displayed on
    the ea?" -- correct, and it mattered.

    The panel had a button reading "HARVEST OFF", which is the TEMPLATE's own
    harvest_enabled flag (PnlB reads the template config). The GLOBAL harvest
    -- g_globalHarvestEnabled, set in Trading > Global Parameters, pushed by
    set_global_config, applied by CheckGlobalHarvest -- was shown nowhere.

    So an armed $75 global harvest was invisible on the very chart it acts on,
    while a button one row down said "HARVEST OFF". Two different settings
    sharing one word on screen is the actual defect.
    """

    def test_the_global_state_is_displayed(self):
        assert "GLOBAL HARVEST" in _code()

    def test_it_shows_the_threshold_not_just_on_off(self):
        """"ON" alone still leaves you unable to tell whether the number the
        app holds is the number the EA got."""
        code = _code()
        ghvst = code[code.index('PnlCell("ghvst"'):]
        ghvst = ghvst[:ghvst.index("y += S;")]

        assert "g_globalHarvestThresholdUsd" in ghvst

    def test_it_reads_the_global_flag_not_the_template_one(self):
        code = _code()
        ghvst = code[code.index('PnlCell("ghvst"'):]
        ghvst = ghvst[:ghvst.index("y += S;")]

        assert "g_globalHarvestEnabled" in ghvst
        assert "PnlB(" not in ghvst

    def test_the_template_button_is_now_labelled_as_the_template_one(self):
        """Renamed so the two cannot be read as the same setting."""
        code = _code()

        assert "TPL HARVEST" in code

    def test_the_global_row_is_read_only(self):
        """It is set in the app and arrives by set_global_config. A button
        here would imply the EA owns it."""
        code = _code()

        assert 'PnlButton("ghvst"' not in code
        assert 'what == "ghvst"' not in code
