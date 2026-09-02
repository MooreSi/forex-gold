"""The Bounce generator is gone from the UI, and cannot be started behind it.

Owner, 2026-09-02: "remove bounce generator, also remove it from the signal
generator".

Removing the tab alone would have been the dangerous half of the job.
`start_stopped_engines()` iterates every registered engine, and the power /
mode toggle calls it — so a Bounce engine with no panel would still be
startable, and it places live MT5 orders when running. An engine that can
trade with nothing on screen showing that it is trading is strictly worse than
one you can see.

So bounce is excluded from the bulk start as well. What is deliberately NOT
done here:

  * the service is not deleted, and
  * the (breakout, bounce, reversal) ordering in `_ENGINE_SERVICES` is not
    touched — the sync server and the mode toggle both bind engines by that
    fixed order, so reordering it would desynchronise paired nodes.

Nothing about breakout or reversal changes.
"""
from __future__ import annotations

import pathlib

import pytest

from backend.src.controllers import engines_controller as ec

REPO = pathlib.Path(__file__).resolve().parents[2]


def _code(rel: str) -> str:
    text = (REPO / rel).read_text(encoding="utf-8")
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.strip().startswith("#"))


class TestTheTabIsGone:
    def test_no_bounce_tab_is_built(self):
        code = _code("frontend/pages/test_panel/__init__.py")
        assert 'ui.tab("Bounce"' not in code

    def test_no_bounce_generator_heading(self):
        assert "Bounce Generator" not in _code(
            "frontend/pages/test_panel/__init__.py")

    def test_breakout_and_reversal_tabs_survive(self):
        code = _code("frontend/pages/test_panel/__init__.py")
        assert 'ui.tab("Breakout"' in code
        assert "Reversal" in code


class TestItCannotBeStartedInvisibly:
    class _Eng:
        def __init__(self):
            self.is_running = False
            self.started = False

        def start(self):
            self.started = True
            self.is_running = True

        def stop(self):
            self.is_running = False

    @pytest.fixture
    def engines(self, monkeypatch):
        made = {name: self._Eng() for name in ("breakout", "bounce", "reversal")}

        class _Svc:
            def __init__(self, eng):
                self._eng = eng

            def get_instance(self):
                return self._eng

        monkeypatch.setattr(
            ec, "_ENGINE_SERVICES",
            {name: _Svc(eng) for name, eng in made.items()})
        return made

    def test_the_bulk_start_does_not_start_bounce(self, engines):
        """It places live MT5 orders. With no panel, nothing on screen would
        say it was running."""
        ec.start_stopped_engines()

        assert engines["bounce"].started is False

    def test_the_bulk_start_still_starts_the_others(self, engines):
        ec.start_stopped_engines()

        assert engines["breakout"].started is True
        assert engines["reversal"].started is True

    def test_the_bulk_stop_still_stops_bounce(self, engines):
        """Asymmetric on purpose: refusing to START it is the safety property,
        refusing to STOP it would strand a running engine."""
        engines["bounce"].is_running = True

        ec.stop_running_engines()

        assert engines["bounce"].is_running is False


class TestTheOrderingIsUntouched:
    def test_the_engine_binding_order_is_unchanged(self):
        """The sync server and the mode toggle bind engines by this fixed
        order. Reordering it desynchronises paired nodes."""
        assert list(ec._ENGINE_SERVICES) == ["breakout", "bounce", "reversal"]
