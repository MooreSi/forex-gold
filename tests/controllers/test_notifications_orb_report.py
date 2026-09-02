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
    def test_the_email_page_no_longer_imports_the_composition_root(self):
        import pathlib

        src = pathlib.Path("frontend/pages/settings/_email.py").read_text(
            encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))

        assert "from backend.src.app import" not in code
        assert "backend.src.app" not in code

    def test_it_is_exported(self):
        assert "build_orb_report" in nc.__all__
