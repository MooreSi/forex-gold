"""Check 4: does an engine method that has an extracted twin actually call it?

No existing test asks this. Each extraction pack shipped a characterization
test (drives the engine method) and a surface test (drives the extracted
function); both pass whether or not the two are connected. That gap is how a
312-line module ended up imported by nothing while its logic still ran inline.
"""
from __future__ import annotations

import ast

from tools.refactor_audit import delegation_checker as dc


def method_named(name: str) -> dict | None:
    for f in dc.audit():
        if f["method"] == name:
            return f
    return None


def test_detects_the_known_unwired_duplicate():
    """_sync_closed_mt5_positions: 273 live lines, extracted module unused."""
    f = method_named("_sync_closed_mt5_positions")
    assert f is not None
    assert f["severity"] == "unwired duplicate"
    assert "core_mt5_position_sync" in f["twins"]


def test_wired_delegators_are_not_flagged():
    """Negative control. These read their implementation from a core_* module.

    _check_sl in particular is reached through an alias
    (`from ...core_monitor_loop import check_sl as _check_sl_impl`), so a
    checker that matched on the local name alone would report it falsely.
    """
    for name in ("_check_sl", "open_trade", "close_trade", "open_trade_from_signal"):
        assert method_named(name) is None, f"{name} delegates and must not be flagged"


def test_suggest_lot_size_duplication_is_still_present():
    """A live risk-control divergence, not scheduled debt.

    engine.py:466 and core_fees_sizing.suggest_lot_size are BOTH reachable and
    they do not agree: only the extracted one applies the
    max_risk_per_trade_pct ceiling (schema default 1.0, i.e. on). Signals
    auto-executed through _scan_messages take the engine copy via
    engine.py:2268/2284 and are sized without that ceiling; manual orders and
    bot commands take the extracted copy and are capped.

    Delete this test when the duplication is resolved -- that deletion is the
    record that a decision was made.
    """
    f = method_named("suggest_lot_size")
    assert f is not None, "the duplicate implementation appears to be resolved"
    assert "core_fees_sizing" in f["twins"]


def test_the_two_lot_sizing_implementations_still_disagree():
    """Pins the specific divergence, so a silent partial fix is visible."""
    engine_src = (dc.ENGINE_PATH).read_text(encoding="utf-8")
    engine_fn = next(
        n for n in ast.walk(ast.parse(engine_src))
        if isinstance(n, ast.FunctionDef) and n.name == "suggest_lot_size"
    )
    extracted_src = (dc.od.CORE_DIR / "core_fees_sizing.py").read_text(encoding="utf-8")
    extracted_fn = next(
        n for n in ast.walk(ast.parse(extracted_src))
        if isinstance(n, ast.FunctionDef) and n.name == "suggest_lot_size"
    )
    assert "max_risk_per_trade_pct" in ast.unparse(extracted_fn)
    assert "max_risk_per_trade_pct" not in ast.unparse(engine_fn), (
        "engine.py's copy now applies the cap -- if this was fixed deliberately, "
        "remove this test and the finding from the Phase 0 docs"
    )


def test_ci_check_fails_while_suggest_lot_size_is_unresolved():
    """The allowlist covers scheduled debt only; a live defect must fail CI."""
    allowed = dc.load_allowlist()
    assert "suggest_lot_size" not in allowed


def parse_fn(src: str) -> ast.FunctionDef:
    return next(n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.FunctionDef))


def test_a_delegating_body_is_a_wrapper():
    assert dc.is_wrapper(parse_fn("def f(self):\n    return impl(self.x)\n"))


def test_a_docstring_does_not_stop_something_being_a_wrapper():
    assert dc.is_wrapper(parse_fn('def f(self):\n    """Doc."""\n    return impl(self.x)\n'))


def test_a_body_with_its_own_logic_is_not_a_wrapper():
    """Short but real: this is the shape suggest_lot_size has."""
    assert not dc.is_wrapper(parse_fn(
        "def f(self, a, b):\n"
        "    d = abs(a - b)\n"
        "    if d <= 0:\n"
        "        return 0.01\n"
        "    return d * 2\n"
    ))
