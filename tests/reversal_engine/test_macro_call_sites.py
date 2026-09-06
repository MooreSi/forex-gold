"""Both feature-building paths must inject the macro context, or neither.

This is a WIRING guard, not a behaviour test. It reads the AST of the two
call sites and checks that the dict handed to `extract_features` is updated
from `re_macro.get_cycle_context()`. It cannot tell you the values are right
-- `test_re_macro.py` and `test_ml_macro_features.py` do that. It exists
because the two paths are 300 lines apart in different files and the failure
mode is silent.

The failure mode: `re_signals` has no macro columns (the vector is persisted
whole as `ml_features_json`), so a signal row read back at fill time carries
no macro at all. If `reversal_engine_live_execute` does not re-read it, the
fill-time re-score falls back to the neutrals in five slots the creation-time
vector filled, and the two disagree -- while that block's own comment claims
it recomputes "every dynamic input". `rsi14` was caught in exactly this way,
which is why the same comment now names it.

It traces the dataflow rather than grepping for the name: a call whose result
is thrown away would pass a grep and fail here.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2] / "backend/src/services/reversal_engine"

CASES = [
    ("reversal_engine_service.py", "feat_input"),
    ("reversal_engine_live_execute.py", "fresh_sig"),
]


def _is_ctx_call(node) -> bool:
    """`await ...get_cycle_context()`, however the module is aliased."""
    if isinstance(node, ast.Await):
        node = node.value
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "get_cycle_context"
    )


def _names_bound_to_the_context(tree) -> set[str]:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _is_ctx_call(node.value):
            out.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return out


def _updates_into(tree, target: str) -> list:
    out = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == target
        ):
            out.extend(node.args)
    return out


@pytest.mark.parametrize("filename,target", CASES)
def test_the_feature_dict_is_updated_from_the_macro_context(filename, target):
    tree = ast.parse((_ROOT / filename).read_text(encoding="utf-8"))
    bound = _names_bound_to_the_context(tree)
    args = _updates_into(tree, target)
    assert args, f"{filename}: nothing is update()d into {target}"

    ok = any(
        _is_ctx_call(a) or (isinstance(a, ast.Name) and a.id in bound)
        for a in args
    )
    assert ok, (
        f"{filename}: {target} is never updated from get_cycle_context(). "
        "The creation-time and fill-time vectors will disagree in the last "
        "five slots."
    )


def test_the_context_is_not_fetched_once_per_candidate(monkeypatch):
    """In the service the fetch must sit OUTSIDE the candidate loop. Inside
    it, a cache miss means blocking HTTP on the engine's event loop once per
    candidate, and the loop also runs position management."""
    tree = ast.parse((_ROOT / "reversal_engine_service.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            inner = [n for n in ast.walk(node) if _is_ctx_call(n)]
            assert not inner, "get_cycle_context() is inside a loop body"
