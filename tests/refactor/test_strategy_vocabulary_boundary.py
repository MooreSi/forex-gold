"""The frontend gets its strategy vocabulary through a controller.

`frontend-reaches-the-backend-through-controllers` counts distinct
(source unit -> imported module) edges, and five frontend units were reaching
straight into `backend.src.utils.models` for strategy ids and display names:
the trading, history and chart packages, plus ai_summary and backtest.

Constants are not a service, so none of them risks calling into a service on
the UI thread -- but the contract counts them, and the architecture's answer to
"where does the frontend get things from" is the same either way: a controller.
Re-exporting here also means a renamed strategy id has one place to change
rather than thirteen import sites.

These assert the boundary AND that the re-export cannot drift from its source,
which is the failure mode a re-export introduces.
"""
from __future__ import annotations

import ast
import pathlib

from backend.src.controllers import history_controller, trading_controller
from backend.src.utils import models
from tools.refactor_audit import import_contracts as ic

REPO = pathlib.Path(__file__).resolve().parents[2]

_STRATEGY_NAMES = [n for n in dir(models) if n.startswith("STRATEGY_")]


def test_the_controller_exposes_the_whole_strategy_vocabulary():
    """Every STRATEGY_* the frontend might need, not a hand-picked subset --
    a partial re-export just sends the next page back to utils.models."""
    missing = [n for n in _STRATEGY_NAMES if not hasattr(trading_controller, n)]
    assert missing == [], f"trading_controller does not re-export: {missing}"


def test_the_re_export_is_the_same_object_not_a_copy():
    """A copied value drifts silently the first time someone edits one side."""
    for name in _STRATEGY_NAMES:
        assert getattr(trading_controller, name) is getattr(models, name), (
            f"{name} in the controller is not the same object as in utils.models"
        )


def test_contract_size_comes_through_the_history_controller():
    assert history_controller.CONTRACT_SIZE is models.CONTRACT_SIZE


def test_the_re_exports_are_declared_public():
    """__all__ is what says these are a deliberate surface rather than a
    leaked import."""
    for name in _STRATEGY_NAMES:
        assert name in trading_controller.__all__, f"{name} is missing from __all__"
    assert "CONTRACT_SIZE" in history_controller.__all__


def test_no_frontend_page_imports_the_models_module_directly():
    """The boundary itself. Constants or not, the frontend's doorway is the
    controller layer."""
    offenders = []
    for path in (REPO / "frontend").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                    "backend.src.utils.models"):
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert offenders == [], (
        "these reach past the controller layer for strategy constants:\n  "
        + "\n  ".join(offenders)
    )


def test_the_contract_total_reflects_the_change():
    """Five source units' worth of edges, gone."""
    count = ic.check().counts["frontend-reaches-the-backend-through-controllers"]
    assert count <= 56, (
        f"{count} edges; removing the utils.models coupling should have taken "
        "this from 61 to 56"
    )
