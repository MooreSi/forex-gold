"""The runtime is a task supervisor and a facade, nothing else (M4 B9e).

By this batch every substantial body has moved to a service. What remains
on the runtime is: __init__ (the state), startup/shutdown (composition),
the ctx builders (the wiring), the facade wrappers (the public API), and
loop shells that own an asyncio task's lifetime and delegate one iteration.

This file pins that shape. It is deliberately structural rather than
behavioural -- the behaviour of each relocated body is covered by its own
characterization pack, which runs unmodified. What structure buys is a
test that fails when a body starts growing back on the runtime, which is
exactly how the previous refactor rotted.

Nothing here touches a broker.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from backend.src.runtime import TradingRuntime

RUNTIME = Path(__file__).resolve().parents[2] / "backend" / "src" / "runtime.py"


def _methods() -> dict[str, ast.AST]:
    tree = ast.parse(RUNTIME.read_text())
    cls = next(n for n in tree.body
               if isinstance(n, ast.ClassDef) and n.name == "TradingRuntime")
    return {n.name: n for n in cls.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


# Bodies that must no longer live on the runtime. Each moved to the service
# named beside it; the runtime keeps a shell that binds and delegates.
RELOCATED = {
    "_is_active_trader_node":         "services.cluster.node_roles",
    "_is_bot_command_authority":      "services.cluster.node_roles",
    "_ai_model_refresh_loop":         "services.ai.model_refresh_loop",
    "_bridge_watchdog_loop":          "services.broker.watchdog_loop",
    "_reversal_engine_research_loop": "services.reversal_engine.research_loop",
    "_tp_ladder_fast_loop":           "services.positions.tp_ladder_loop",
    "_bot_command_loop":              "services.telegram.bot_loop",
}

# A shell binds collaborators and delegates. Anything longer is a body that
# did not actually move. Generous ceiling: the biggest legitimate shell is
# the bot loop's, which builds deps and owns a poll interval.
MAX_SHELL_LINES = 22


@pytest.mark.parametrize("name,service", sorted(RELOCATED.items()))
def test_the_relocated_bodies_are_shells_now(name, service):
    methods = _methods()
    assert name in methods, f"{name} vanished entirely -- it should still be a shell"
    node = methods[name]
    length = node.end_lineno - node.lineno + 1
    assert length <= MAX_SHELL_LINES, (
        f"{name} is {length} lines -- a shell should bind and delegate. Its "
        f"body belongs in {service}."
    )


@pytest.mark.parametrize("name,service", sorted(RELOCATED.items()))
def test_each_relocated_service_module_exists_and_is_importable(name, service):
    __import__(f"backend.src.{service}")


def test_the_static_node_role_checks_still_answer_from_the_class():
    """Both are patched by name on the class in several characterization
    packs, so they must remain class attributes that take no self."""
    for name in ("_is_active_trader_node", "_is_bot_command_authority"):
        attr = TradingRuntime.__dict__[name]
        assert isinstance(attr, staticmethod), f"{name} must stay a staticmethod"
        assert isinstance(getattr(TradingRuntime, name)(), bool)


def test_the_loop_shells_are_still_coroutines():
    """Negative control: shells own asyncio tasks; app.py creates them."""
    for name in ("_monitor_loop", "_bot_command_loop", "_tp_ladder_fast_loop",
                 "_bridge_watchdog_loop", "_ai_model_refresh_loop",
                 "_reversal_engine_research_loop"):
        assert asyncio.iscoroutinefunction(getattr(TradingRuntime, name)), name


def test_the_facade_and_the_state_are_deliberately_still_here():
    """Negative control: this batch dissolves bodies, not the design.

    __init__ holds the task handles and caches, startup/shutdown compose,
    and the ctx builders are the single binding site the whole refactor is
    built around. A test that only checked 'things got smaller' would pass
    just as happily if these were deleted.
    """
    for survivor in ("__init__", "startup", "shutdown",
                     "_make_close_trade_ctx", "_make_scan_ctx",
                     "_make_monitor_ctx", "_make_position_sync_ctx",
                     "_make_bot_deps", "close_trade", "open_trade", "get_tick"):
        assert survivor in _methods() or hasattr(TradingRuntime, survivor), survivor
