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


def test_no_unwired_duplicates_remain():
    """Every engine method with an extracted twin now calls it.

    Five did not, all blocked on the same partial CloseTradeContext. They were
    resolved by deleting the extracted copies (2026-07-27) rather than wiring
    them, so those methods no longer have a twin to disagree with. If this
    starts failing, an extraction has been merged dead again -- which is the
    exact failure this checker exists to catch.
    """
    assert [f for f in dc.audit() if f["severity"] == "unwired duplicate"] == []


def test_wired_delegators_are_not_flagged():
    """Negative control. These read their implementation from a core_* module.

    _check_sl in particular is reached through an alias
    (`from ...core_monitor_loop import check_sl as _check_sl_impl`), so a
    checker that matched on the local name alone would report it falsely.
    """
    for name in ("_check_sl", "open_trade", "close_trade", "open_trade_from_signal"):
        assert method_named(name) is None, f"{name} delegates and must not be flagged"


def test_lot_sizing_has_exactly_one_implementation():
    """Regression guard on a fixed live defect.

    engine.py used to carry its own copy of the sizing maths. When Global
    Parameters > Max Risk per trade % was added to core_fees_sizing and not to
    that copy, the two silently disagreed -- and because _scan_messages injects
    the engine method as suggest_lot_size_fn (engine.py:2268/2284), Telegram
    auto-executed signals were sized without a ceiling that manual orders and
    bot commands applied. Same UI field, honoured on two entry paths of three.

    The engine method must stay a pure delegation. Reintroducing arithmetic
    here is how the divergence happened the first time.
    """
    assert method_named("suggest_lot_size") is None, (
        "engine.py's suggest_lot_size no longer delegates to core_fees_sizing"
    )


def test_only_the_extracted_copy_owns_the_risk_ceiling():
    engine_fn = next(
        n for n in ast.walk(ast.parse(dc.ENGINE_PATH.read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef) and n.name == "suggest_lot_size"
    )
    extracted_fn = next(
        n for n in ast.walk(ast.parse(
            (dc.od.REPO_ROOT / "backend/src/services/trading/fees_sizing.py").read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef) and n.name == "suggest_lot_size"
    )
    assert "max_risk_per_trade_pct" in ast.unparse(extracted_fn)
    assert dc.is_wrapper(engine_fn), "the engine method must remain a plain delegation"


def test_the_scan_path_and_the_manual_path_now_size_identically():
    """The actual behavioural claim, checked end to end rather than by shape.

    engine.py:2268/2284 hand `self.suggest_lot_size` to the scan path; manual
    orders call core_fees_sizing.suggest_lot_size directly. Those two must
    produce the same number for the same inputs, or the fork is back.
    """
    import types
    from backend.src.services.trading import fees_sizing as core_fees_sizing

    captured = {}

    def fake_get_risk_settings():
        captured["called"] = True
        return {"max_lot_size": 0.10, "max_risk_per_trade_pct": 1.0}

    original = core_fees_sizing.db_module.get_risk_settings
    core_fees_sizing.db_module.get_risk_settings = fake_get_risk_settings
    try:
        engine_like = types.SimpleNamespace()
        from forex_trader.core.engine import SimulationEngine
        scan_path = SimulationEngine.suggest_lot_size(
            engine_like, 2000.0, 1990.0, 10_000.0, 5.0)
        manual_path = core_fees_sizing.suggest_lot_size(
            2000.0, 1990.0, 10_000.0, 5.0)
    finally:
        core_fees_sizing.db_module.get_risk_settings = original

    assert scan_path == manual_path
    assert captured.get("called"), "the engine path never consulted risk settings"


def test_suggest_lot_size_is_not_in_the_allowlist():
    """It was a live defect, never scheduled debt. It must not reappear here."""
    assert "suggest_lot_size" not in dc.load_allowlist()


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
