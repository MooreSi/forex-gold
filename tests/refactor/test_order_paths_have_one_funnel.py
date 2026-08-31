"""Why the stage-3 gates are sound, expressed as structure rather than trust.

On 2026-08-31, driving 020's killer demo showed its fix had been wired into
one signal route and not the parallel one, so the guard never ran on the path
most signals take. That prompted an audit of the other four. They came out
clean -- but for a reason nothing was pinning:

    010 (no duplicate order)   `bridge.place_order` has exactly ONE call site,
                               so the dedup gate cannot be bypassed by a route
                               that opens trades some other way.

    050 (protective halts)     the halt check sits in `open_trade` BEFORE the
                               EA handoff, so it covers the EA path and the
                               Python-bridge path both -- rather than being
                               repeated per caller, which is what 020 got
                               wrong.

Both are properties of the shape of the code, and both would break silently.
A second `place_order` call site added for some new engine would place orders
that no dedup check ever sees; moving the halt check below the EA handoff
would let a halted account keep trading through the EA alone. Neither would
fail an existing test.

These are structural tests: they read the tree, they run no orders, and they
are cheap. `mt5_bridge.py` and the broker CLIENTS are excluded -- they define
and forward `place_order`; they are the plumbing, not a trading decision.
"""
from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
BACKEND = REPO / "backend" / "src"

# The bridge implementations themselves, and the fake that mirrors them.
# Calling place_order here IS the definition of placing an order, not a
# decision to place one.
_PLUMBING = {
    "backend/src/services/broker/mt5_client.py",
    "backend/src/services/broker/mt5_native.py",
    "backend/src/services/broker/fake_bridge.py",
}


def _py_files():
    for p in BACKEND.rglob("*.py"):
        if "__pycache__" in p.parts:
            continue
        yield p


def _rel(p: pathlib.Path) -> str:
    return str(p.relative_to(REPO)).replace("\\", "/")


def _calls_named(tree: ast.AST, name: str) -> list:
    """Every `<something>.<name>(...)` call, as (lineno, ...) pairs."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == name:
                out.append(node)
    return out


def test_place_order_has_exactly_one_call_site():
    """stage3/010's dedup gate guards the single funnel. A second funnel would
    be an order path with no duplicate check at all."""
    sites = []
    for path in _py_files():
        rel = _rel(path)
        if rel in _PLUMBING:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _calls_named(tree, "place_order"):
            sites.append(f"{rel}:{call.lineno}")

    assert len(sites) == 1, (
        f"`place_order` is called from {len(sites)} places: {sites}. "
        f"stage3/010's dedup gate sits on ONE of them. A second call site is an "
        f"order path that no duplicate check protects -- put the send behind "
        f"open_trade, or extend the gate and update this test deliberately."
    )
    # Named, not just counted: one call site in the wrong module would satisfy
    # the count and bypass the gate just as thoroughly.
    assert sites[0].startswith("backend/src/services/trading/open_trade.py:"), sites


def test_the_halt_check_comes_before_the_ea_handoff():
    """stage3/050. `open_trade` is the funnel for BOTH send paths, and the
    protective halt has to gate both. If the check drifts below the EA handoff,
    a halted account keeps trading -- through the EA only, which is the harder
    case to notice."""
    src = (BACKEND / "services" / "trading" / "open_trade.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    halt_lines = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id == "is_trading_paused"
    ]
    # The import is at module level; the check is the one inside the function.
    halt_lines = [ln for ln in halt_lines if ln > 100]
    assert halt_lines, "open_trade no longer checks is_trading_paused at all"

    ea_handoff = [
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "open_trade"
        and isinstance(n.func.value, ast.Name) and n.func.value.id == "_ea"
    ]
    assert ea_handoff, "the EA handoff moved -- this test needs rewriting, not deleting"

    assert min(halt_lines) < min(ea_handoff), (
        f"the halt check (line {min(halt_lines)}) now runs AFTER the EA handoff "
        f"(line {min(ea_handoff)}). A halted account would still trade via the EA."
    )


def test_the_halt_check_comes_before_the_bridge_send():
    """The other half of the same property."""
    src = (BACKEND / "services" / "trading" / "open_trade.py").read_text(encoding="utf-8")
    tree = ast.parse(src)

    halt_lines = [ln for ln in (
        n.lineno for n in ast.walk(tree)
        if isinstance(n, ast.Name) and n.id == "is_trading_paused"
    ) if ln > 100]
    sends = [n.lineno for n in _calls_named(tree, "place_order")]

    assert halt_lines and sends
    assert min(halt_lines) < min(sends), (
        "the halt check now runs after the bridge send"
    )


def test_the_scanner_would_notice_a_second_send():
    """Negative control. Every assertion above rests on `_calls_named` finding
    what is there; a walker that found nothing would make all three vacuous."""
    planted = ast.parse(
        "async def sneaky(bridge):\n"
        "    await bridge.place_order('BUY', 0.1, None, None, '')\n"
    )

    found = _calls_named(planted, "place_order")

    assert len(found) == 1
    assert found[0].lineno == 2
