"""A pushed template re-points only trades opened under THAT template.

`HandleSetTemplate` in ForexTraderBridge.mq5 is what a template push lands on:
the dashboard's Save, the panel's context push when the EA connects, and every
CH tab click on the chart. Until EA v1.09 it rewrote the stored config of every
open template trade, whichever template it was opened under -- so saving
"GD VIP - Single" put open "GD VIP - Grid" trades on Single's TP, breakeven and
trail rules, and merely clicking a CH tab did the same with that tab's template.
Found 2026-10-01 while checking that Save reaches the EA.

The EA has no test harness, so this reads the function's source. Nothing here
reaches a broker, a bridge or an EA.
"""
from __future__ import annotations

import re
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[2] / "mql5" / "ForexTraderBridge.mq5").read_text(
    encoding="utf-8", errors="ignore")


def _body(name: str) -> str:
    start = _SRC.index(f"void {name}(")
    open_at = _SRC.index("{", start)
    depth = 0
    for i in range(open_at, len(_SRC)):
        depth += {"{": 1, "}": -1}.get(_SRC[i], 0)
        if depth == 0:
            return _SRC[open_at:i + 1]
    raise AssertionError(f"{name} has no closing brace")


def _loops(body: str) -> list[str]:
    """Each for-loop's own block in the function, in order."""
    out = []
    for m in re.finditer(r"for\s*\(", body):
        open_at = body.index("{", m.end())
        depth = 0
        for i in range(open_at, len(body)):
            depth += {"{": 1, "}": -1}.get(body[i], 0)
            if depth == 0:
                out.append(body[m.start():i + 1])
                break
    return out


def test_open_trades_are_filtered_by_the_pushed_templates_name():
    loops = [lp for lp in _loops(_body("HandleSetTemplate")) if "g_trades" in lp]
    assert loops, "HandleSetTemplate no longer walks g_trades -- re-read this test"
    for lp in loops:
        assert re.search(r'strategy\s*!=\s*"template:"\s*\+\s*name', lp) or \
               re.search(r'strategy\s*!=\s*want', lp), lp


def test_resting_orders_are_filtered_the_same_way():
    """A resting grid leg carries its config into the fill. Left unfiltered,
    the wrong template would arrive the moment it filled instead."""
    loops = [lp for lp in _loops(_body("HandleSetTemplate")) if "g_pending" in lp]
    assert loops
    for lp in loops:
        assert re.search(r'strategy\s*!=\s*"template:"\s*\+\s*name', lp) or \
               re.search(r'strategy\s*!=\s*want', lp), lp


def test_the_panel_still_shows_the_pushed_template():
    """The filter is on the trades only: the panel display is what a CH tab
    click is for, and it must still follow every push."""
    body = _body("HandleSetTemplate")
    assert "g_panelTemplate = name" in body
    assert "g_panelCfg      = json" in body or "g_panelCfg = json" in body


def test_the_filter_has_teeth():
    """Negative control: the unfiltered v1.08 loop must fail the check above."""
    old = """for(int i = 0; i < ArraySize(g_trades); i++)
   {
      if(!g_trades[i].isTemplate) continue;
      g_trades[i].tplCfg            = json;
   }"""
    assert not re.search(r'strategy\s*!=\s*"template:"\s*\+\s*name', old)
    assert not re.search(r'strategy\s*!=\s*want', old)
