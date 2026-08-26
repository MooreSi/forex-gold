"""The AI-analysis repo resolves the names it uses.

`backend/src/services/analytics/ai_analysis_repo.py` says at the top that its
functions were "moved verbatim from frontend/pages/ai_trade_analysis.py (M3
page drain)". The functions came across; four module-scope names they depend on
did not:

    datetime, timezone      used by _session_from_ts
    _TP_HIT_RE, _SL_HIT_RE  used by _gather_channel_data

They are still sitting in the page the code was moved out of. The module
imports cleanly, the controller wires up fine, and nothing fails until the code
actually runs -- which is exactly the failure mode
`tests/frontend/test_page_packages_are_wired.py` was written for, except that
gate only covers page packages under frontend/pages/, not backend services.

Found 2026-08-26 by running pyflakes across the whole tree.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
MODULE = REPO / "backend" / "src" / "services" / "analytics" / "ai_analysis_repo.py"


def _unresolved(path: Path) -> set[str]:
    """Global names a module's functions load but nothing in it binds."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bound = set(dir(builtins)) | {"__name__", "__file__", "__doc__"}
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound |= {a.asname or a.name.split(".")[0] for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)

    missing = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in bound:
            missing.add(node.id)
    return missing


def test_every_name_the_repo_uses_resolves():
    assert _unresolved(MODULE) == set(), (
        "these names are used but never bound -- the functions were moved "
        "without their module-scope dependencies"
    )


def test_session_from_ts_actually_runs():
    """The regression, executed rather than inspected.

    _session_from_ts is reached from _gather_channel_data on every signal that
    has a parsed_at, so this is not a corner.
    """
    from backend.src.services.analytics import ai_analysis_repo as repo

    assert repo._session_from_ts(None) == "Unknown"
    # 03:00 UTC -> Asian. Any real timestamp exercises the datetime path.
    assert repo._session_from_ts(3 * 3600) == "Asian"
    assert repo._session_from_ts(9 * 3600) == "London"


def test_the_tp_and_sl_patterns_are_present_and_work():
    """_gather_channel_data scans reply chains with these two regexes."""
    from backend.src.services.analytics import ai_analysis_repo as repo

    assert repo._TP_HIT_RE.search("TP1 hit") is not None
    assert repo._TP_HIT_RE.search("nothing here") is None
    assert repo._SL_HIT_RE.search("stop loss hit") is not None
    assert repo._SL_HIT_RE.search("nothing here") is None


def test_the_name_check_would_notice_a_missing_name(tmp_path):
    """Negative control for the scan above."""
    bad = tmp_path / "bad.py"
    bad.write_text("def f():\n    return _never_bound\n", encoding="utf-8")
    assert "_never_bound" in _unresolved(bad)

    good = tmp_path / "good.py"
    good.write_text("X = 1\n\n\ndef f():\n    return X\n", encoding="utf-8")
    assert _unresolved(good) == set()
