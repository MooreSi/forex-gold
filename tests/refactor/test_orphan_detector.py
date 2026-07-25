"""The orphan detector is only useful if it neither over- nor under-reports.

Under-reporting hides dead code (the failure that let core_mt5_position_sync
sit unwired behind a green suite). Over-reporting trains people to ignore it.
The alias cases below are the ones a text grep gets wrong.
"""
from __future__ import annotations

import ast

import pytest

from tools.refactor_audit import orphan_detector as od


def usage_of(src: str) -> set[tuple[str, str]]:
    collector = od.UsageCollector()
    collector.visit(ast.parse(src))
    return collector.used


def test_direct_symbol_import_is_a_use():
    assert ("core_open_trade", "open_trade") in usage_of(
        "from forex_trader.core.core_open_trade import open_trade"
    )


def test_aliased_module_attribute_is_a_use():
    # ui/pages/trading.py:1434 does exactly this.
    used = usage_of(
        "from forex_trader.core import core_trading_schedule as sched\n"
        "sched.is_session_allowed('GOLD')\n"
    )
    assert ("core_trading_schedule", "is_session_allowed") in used


def test_unaliased_module_import_then_attribute_is_a_use():
    used = usage_of(
        "from forex_trader.core import core_signals\n"
        "core_signals.get_signal('x')\n"
    )
    assert ("core_signals", "get_signal") in used


def test_dotted_import_with_alias_is_a_use():
    used = usage_of(
        "import forex_trader.core.core_risk_governor as rg\n"
        "rg.rg_check_halt()\n"
    )
    assert ("core_risk_governor", "rg_check_halt") in used


def test_keyword_argument_of_the_same_name_is_not_a_use():
    """The false positive a text grep produces.

    Nine handler modules take `close_full_after_tps` as a keyword argument.
    That is a parameter name, not a call into core_profit_sync.
    """
    used = usage_of(
        "run_tp_ladder(bridge, close_full_after_tps=some_callable)"
    )
    assert ("core_profit_sync", "close_full_after_tps") not in used


def test_attribute_on_an_unknown_name_is_not_a_use():
    assert usage_of("self.close_full_after_tps()") == set()


def test_getattr_with_a_literal_is_a_use():
    used = usage_of(
        "from forex_trader.core import core_signals\n"
        "getattr(core_signals, 'get_signal')\n"
    )
    assert ("core_signals", "get_signal") in used


def test_getattr_with_a_dynamic_name_marks_the_whole_module_reachable():
    """We can't resolve it, so we must not claim anything in it is dead."""
    used = usage_of(
        "from forex_trader.core import core_signals\n"
        "getattr(core_signals, name)\n"
    )
    assert ("core_signals", "*") in used


def test_star_import_marks_the_whole_module_reachable():
    assert ("core_signals", "*") in usage_of(
        "from forex_trader.core.core_signals import *"
    )


def test_tests_directory_is_not_production():
    """A module reachable only from its own test is precisely an orphan."""
    files = {p.relative_to(od.REPO_ROOT).parts[0] for p in od.production_files()}
    assert "tests" not in files


def test_public_functions_skips_private_and_reports_loc(tmp_path):
    mod = tmp_path / "core_sample.py"
    mod.write_text(
        "def public_one():\n"
        "    a = 1\n"
        "    return a\n"
        "\n"
        "def _private():\n"
        "    return 2\n"
    )
    assert od.public_functions(mod) == [("public_one", 1, 3)]


@pytest.mark.parametrize("ident", [
    "core_mt5_position_sync::sync_closed_mt5_positions",
    "core_profit_sync::close_full_after_tps",
    "core_fees_sizing::calculate_fees",
    "core_tp_trigger_tracking::check_sl",
])
def test_known_orphans_are_still_detected(ident):
    """Regression guard on the four confirmed unwired extractions.

    When one is genuinely wired in or deleted, remove it from this list in the
    same commit -- that edit is the proof the work happened.
    """
    found = {f"{o['module']}::{o['function']}" for o in od.find_orphans()}
    assert ident in found


def test_wired_extractions_are_not_reported():
    """Negative control: these are called from engine.py and must stay clean."""
    found = {f"{o['module']}::{o['function']}" for o in od.find_orphans()}
    for ident in ("core_open_trade::open_trade",
                  "core_close_trade::close_trade",
                  "core_monitor_loop::check_sl",
                  "core_risk_governor::rg_check_halt"):
        assert ident not in found
