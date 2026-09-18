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

**The re-export itself went on 2026-09-18**, and the reason is the whole point
of this file: those five frontend units were Python, and the frontend is now a
browser. It cannot import `utils.models` — or anything else — so the coupling
this was written to remove cannot come back in that form, and eighteen
constants nothing called were sitting in a controller holding it over its
200-line ceiling.

The requirement did not go anywhere. The top layer still gets the vocabulary
through a controller: `build_strategy_catalogue()` hands it to the browser as
JSON, and `the-api-layer-reaches-the-backend-through-controllers` is enforced
at zero with no baseline. Those two are asserted below instead.

`CONTRACT_SIZE` stays re-exported through `history_controller` and is still
checked for drift, because that one has a caller.
"""
from __future__ import annotations

import ast
import pathlib

from backend.src.controllers import history_controller, trading_controller
from backend.src.utils import models
from tools.refactor_audit import import_contracts as ic

REPO = pathlib.Path(__file__).resolve().parents[2]

_STRATEGY_NAMES = [n for n in dir(models) if n.startswith("STRATEGY_")]


def test_the_browser_gets_the_vocabulary_as_data():
    """What replaced the re-export. The strategy names and descriptions reach
    the dashboard as JSON from an operation, so a renamed id still has one
    place to change -- and the browser never sees a Python name at all."""
    catalogue = trading_controller.build_strategy_catalogue()

    assert catalogue, "the catalogue is empty, so no screen can offer a strategy"
    assert "build_strategy_catalogue" in trading_controller.__all__


def test_the_api_layer_does_not_reach_for_the_models_module_itself():
    """The boundary, at the layer that now sits on top. A router importing
    `utils.models` for a strategy id is the same coupling in a new place."""
    offenders = []
    for path in (REPO / "backend" / "src" / "api").rglob("*.py"):
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


def test_the_strategy_vocabulary_is_no_longer_re_exported():
    """Exact, so it cannot creep back one constant at a time.

    Eighteen of these were re-exported for Python pages that no longer exist,
    and nothing called any of them through the controller. They were invisible
    to `test_controller_operations_have_callers` because its scan is a
    substring search over `backend/`, and every name appears there in the
    service that genuinely uses it.
    """
    still_there = [n for n in _STRATEGY_NAMES if n in trading_controller.__all__]

    assert still_there == [], (
        f"{still_there} are back on the controller. Services import them from "
        "utils.models directly; the browser gets the catalogue as JSON."
    )


def test_contract_size_comes_through_the_history_controller():
    assert history_controller.CONTRACT_SIZE is models.CONTRACT_SIZE


def test_the_remaining_re_export_is_declared_public():
    """__all__ is what says a re-export is a deliberate surface rather than a
    leaked import."""
    assert "CONTRACT_SIZE" in history_controller.__all__


def test_there_is_no_python_frontend_left_to_check():
    """This file used to scan `frontend/**/*.py` for the same import. There is
    no Python under `frontend/` any more, so that scan found nothing and would
    have gone on finding nothing however badly the boundary was broken --
    a check that passes over an empty set.

    Asserting the emptiness directly is the honest version: the day a .py file
    appears there, this says so and the scan above is the one to extend.
    """
    stray = [p for p in (REPO / "frontend").rglob("*.py")
             if "node_modules" not in p.parts and "__pycache__" not in p.parts]

    assert stray == [], f"Python has reappeared under frontend/: {stray}"


def test_the_contract_total_reflects_the_change():
    """The contract this counted reached zero, then moved.

    It was `frontend-reaches-the-backend-through-controllers`, and the point of
    the assertion was that removing five units' worth of `utils.models`
    coupling took the count from 61 to 56. It carried on down to 0 on
    2026-09-02, and on 2026-09-18 the top layer moved from `frontend/` to
    `backend/src/api/` and the contract moved with it, renamed.

    The claim worth keeping is the stronger one the count was heading towards:
    the top layer gets its strategy vocabulary through a controller, and there
    is no allowance for anything else.
    """
    count = ic.check().counts["the-api-layer-reaches-the-backend-through-controllers"]
    assert count == 0, f"{count} edge(s) reaching past the controllers"
