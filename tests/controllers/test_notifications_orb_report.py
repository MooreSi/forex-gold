"""The ORB report reaches the page through a controller, not around one.

restructure phase1/060. `frontend/pages/settings/_email.py` built the scheduled
ORB report by importing `backend.src.app` directly for the runtime handle --
one of the last two sites keeping the frontend contract off zero.

The rest of that email's path was already behind `notifications_controller`
(`build_orb_chart_image`, `build_orb_html`, `send_email`). Only the report
itself reached around it, so the page needed the composition root in scope to
send an email. Routed, not altered.
"""
from __future__ import annotations

import pytest

from backend.src.controllers import notifications_controller as nc


class _Engine:
    def __init__(self, report=None, boom=False):
        self._report = report
        self._boom = boom
        self.calls = 0

    async def build_orb_report(self):
        self.calls += 1
        if self._boom:
            raise RuntimeError("bridge down")
        return self._report


@pytest.mark.asyncio
class TestItForwardsToTheRuntime:
    async def test_the_report_comes_back(self, monkeypatch):
        engine = _Engine(report={"symbol": "XAUUSD"})
        monkeypatch.setattr(nc, "_get_engine", lambda: engine)

        assert await nc.build_orb_report() == {"symbol": "XAUUSD"}

    async def test_the_runtime_is_asked_exactly_once(self, monkeypatch):
        engine = _Engine(report={})
        monkeypatch.setattr(nc, "_get_engine", lambda: engine)

        await nc.build_orb_report()

        assert engine.calls == 1

    async def test_no_runtime_yet_is_not_a_crash(self, monkeypatch):
        """The page calls this from a button. Before startup completes, or on a
        node running headless, there is no engine -- the page's own "could not
        build report" branch should handle it, not a traceback."""
        monkeypatch.setattr(nc, "_get_engine", lambda: None)

        assert await nc.build_orb_report() is None

    async def test_a_broken_bridge_is_not_swallowed(self, monkeypatch):
        """`None` means "no report"; it must not also mean "the bridge threw".
        The page shows a different message for each."""
        monkeypatch.setattr(nc, "_get_engine", lambda: _Engine(boom=True))

        with pytest.raises(RuntimeError):
            await nc.build_orb_report()


class TestThePageGoesThroughIt:
    def test_the_top_layer_no_longer_imports_the_composition_root(self):
        """Retargeted 2026-09-18 from the NiceGUI email settings module to
        `backend/src/api/`. The rule is the same and is now a contract enforced
        at zero, with `server.py` as the single named exemption — it IS the
        composition root, so it is the one file allowed to name one."""
        import pathlib

        api = pathlib.Path(__file__).resolve().parents[2] / "backend" / "src" / "api"
        sources = [p for p in api.rglob("*.py") if p.name != "server.py"]
        assert sources, "the API layer has no Python in it — this scan is inert"

        # Parsed, not grepped. The NiceGUI version stripped `#` comments and
        # searched the text, which would now fail on a module DOCSTRING that
        # merely explains why only server.py may import the composition root.
        # A string that mentions a rule is not a breach of it; an import is.
        import ast

        for path in sources:
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom) and node.module:
                    assert not node.module.startswith("backend.src.app"), (
                        f"{path.name}:{node.lineno}")
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        assert not alias.name.startswith("backend.src.app"), (
                            f"{path.name}:{node.lineno}")

    def test_it_is_exported(self):
        assert "build_orb_report" in nc.__all__
