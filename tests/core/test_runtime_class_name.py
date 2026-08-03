"""The class is called TradingRuntime (M4, final step).

`SimulationEngine` predates the app doing real broker work. It has not
simulated anything for a long time: it places live orders, reconciles
against MT5, and supervises thirteen asyncio tasks. A name describing the
object as a simulator is actively misleading in a codebase where the
difference between simulated and real is the difference that matters.

The rename was deliberately held until last. Doing it mid-dissolution
would have touched every characterization test in the same commits that
were moving code, burying the real diffs under 600 identifier changes.

Historical prose is NOT renamed. Docstrings saying "extracted from
core/engine.py's SimulationEngine._x" describe a file and a class that
genuinely had those names at the time; rewriting them would falsify the
audit trail this whole refactor depends on.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "backend" / "src" / "runtime.py"


def test_the_class_is_named_trading_runtime():
    from backend.src.runtime import TradingRuntime
    assert TradingRuntime.__name__ == "TradingRuntime"


def test_the_old_name_still_resolves_for_callers_that_have_not_moved():
    """A compatibility alias, not a second class. Anything still holding
    the old name gets the same object, so the rename cannot half-apply."""
    import backend.src.runtime as runtime
    assert runtime.SimulationEngine is runtime.TradingRuntime


def test_the_class_definition_itself_uses_the_new_name():
    tree = ast.parse(RUNTIME.read_text())
    classes = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    assert "TradingRuntime" in classes
    assert "SimulationEngine" not in classes, (
        "SimulationEngine must be an alias, not a class definition"
    )


def test_the_facade_audit_follows_the_rename():
    """facade_audit keys its census off one class-name constant precisely
    so this rename is a one-line change there."""
    from tools.refactor_audit import facade_audit
    assert facade_audit.CLASS_NAME == "TradingRuntime"
    census = facade_audit.census(RUNTIME.read_text())
    assert census, "the audit found no methods -- it is looking at the wrong class"
