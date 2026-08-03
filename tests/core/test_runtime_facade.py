"""The runtime's public API is the whole API (M4 B8).

Seven methods carried a leading underscore while being called from outside
runtime.py -- the frontend pages, the sync server, the EA bridge, the self
healer. An underscore that six production modules ignore is not
encapsulation, it is a lie about the boundary: facade_audit's allowlist
cannot enforce a contract that a third of the real surface hides from.

B8 promotes those seven to public names and rewires every external caller.
Each promotion is a rename and nothing else -- same body, same bindings,
same *_impl call -- so no behaviour moves in this batch.

This file pins both halves. The durable one is
test_no_production_code_reaches_into_a_runtime_private: it is not a list
of the seven, it derives the leak set from SimulationEngine's own members,
so a private this batch never heard of fails it the day it leaks.

Nothing here places, closes or modifies an order. The one behavioural test
drives the EA bridge against a stub engine whose record_close is a
recorder.
"""
from __future__ import annotations

import ast
import asyncio
import json
import re
from pathlib import Path

import pytest

from backend.src.runtime import TradingRuntime

REPO_ROOT = Path(__file__).resolve().parents[2]

# old private name -> the public name it was promoted to.
PROMOTED = {
    "_get_triggered_tps":               "get_triggered_tps",
    "_sync_profit":                     "sync_profit",
    "_background_open_commentary":      "background_open_commentary",
    "_apply_followup_to_instant_trade": "apply_followup_to_instant_trade",
    # Close path: renamed only. The body, the ctx it builds and the impl it
    # calls are byte-identical -- M4 does not reshape closing.
    "_record_close":                    "record_close",
    # Found by the leak scan below, not by the plan: same class of fix, one
    # external caller each (self_healer, the remote-node controller).
    "_start_bridge_process":            "start_bridge_process",
    "_cmd_restart_app":                 "restart_app",
}


@pytest.mark.parametrize("old,new", sorted(PROMOTED.items()))
def test_promoted_names_exist_and_privates_gone(old, new):
    assert hasattr(TradingRuntime, new), (
        f"{new} is the promoted name for {old} -- external callers bind to it."
    )
    assert not hasattr(TradingRuntime, old), (
        f"{old} was promoted to {new} in M4 B8; the private alias must not "
        f"survive, or callers keep reaching through it."
    )


def _runtime_privates() -> set[str]:
    return {n for n in vars(TradingRuntime)
            if n.startswith("_") and not n.startswith("__")}


def _docstring_lines(tree: ast.AST) -> set[int]:
    """Line numbers covered by docstrings -- prose that names a method is
    not a call of it, and this refactor's docstrings name plenty."""
    lines: set[int] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            lines.update(range(node.lineno, (node.value.end_lineno or node.lineno) + 1))
    return lines


def _find_private_reaches() -> list[str]:
    # A call on a receiver spelled like an engine handle -- `engine`,
    # `eng`, `self._engine`, `self._main_engine` -- naming a method that
    # SimulationEngine actually defines as private.
    pattern = re.compile(r"\b(?:\w*[Ee]ngine|eng)\.(_\w+)\s*\(")
    privates = _runtime_privates()
    offenders: list[str] = []
    for root in ("backend", "frontend"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if path.name == "runtime.py" or "__pycache__" in path.parts:
                continue
            source = path.read_text()
            try:
                docs = _docstring_lines(ast.parse(source))
            except SyntaxError:  # pragma: no cover - repo must parse
                docs = set()
            for lineno, line in enumerate(source.splitlines(), 1):
                if lineno in docs or line.lstrip().startswith("#"):
                    continue
                for match in pattern.finditer(line):
                    if match.group(1) in privates:
                        offenders.append(
                            f"{path.relative_to(REPO_ROOT)}:{lineno} -> {match.group(1)}"
                        )
    return offenders


def test_no_production_code_reaches_into_a_runtime_private():
    offenders = _find_private_reaches()
    assert offenders == [], (
        "production code is calling a private method on the runtime:\n  "
        + "\n  ".join(offenders)
        + "\nPromote the method and add it to facade_allowlist.json instead."
    )


def test_the_leak_scan_can_actually_see_a_leak():
    """Negative control: a zero-offender assertion is worthless if the
    scanner is blind. The seven names B8 promoted were all found by it."""
    assert _runtime_privates(), "census found no privates at all -- scanner broken"
    pattern = re.compile(r"\b(?:\w*[Ee]ngine|eng)\.(_\w+)\s*\(")
    assert pattern.search("triggered = await engine._get_triggered_tps(tid)")
    assert pattern.search("await self._main_engine._some_private(a, b)")
    assert not pattern.search("await self._bridge._get_positions()")


def test_the_promoted_names_are_all_allowlisted():
    """A promoted name that skips the allowlist defeats the facade audit."""
    allowlist = json.loads(
        (REPO_ROOT / "tools" / "refactor_audit" / "facade_allowlist.json").read_text()
    )
    for new in PROMOTED.values():
        assert new in allowlist, f"{new} is public now -- allowlist it."


def test_the_ea_bridge_closes_through_the_public_name():
    """Behavioural wiring: EA 'trade_closed' -> engine.record_close.

    The engine is a stub and record_close is a recorder. No bridge, no
    broker, no order.
    """
    from backend.src.services.broker import ea_bridge

    calls = []

    class _StubEngine:
        async def record_close(self, trade_id, close_price, reason):
            calls.append((trade_id, close_price, reason))
            return {"net_pnl": 0.0}

        async def get_mt5_account(self):
            return {}

    bridge = ea_bridge.EABridge.__new__(ea_bridge.EABridge)
    bridge._engine = _StubEngine()
    bridge._active = {}

    async def _fetch_trade(trade_id):
        return {"trade_id": trade_id}

    bridge._fetch_trade = _fetch_trade

    asyncio.run(bridge._on_trade_closed(
        {"trade_id": "t-1", "close_price": 2410.0, "reason": "EA_close"}
    ))

    assert calls == [("t-1", 2410.0, "EA_close")]


def test_the_close_path_bindings_are_untouched():
    """Negative control: promotion is a rename, not a reshaping."""
    assert hasattr(TradingRuntime, "_make_close_trade_ctx")
    assert hasattr(TradingRuntime, "close_trade")
    assert hasattr(TradingRuntime, "_schedule_profit_sync")
