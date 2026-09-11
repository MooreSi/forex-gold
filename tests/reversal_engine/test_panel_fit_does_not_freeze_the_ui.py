"""Enabling "Learn From Pro Signals" must not freeze the whole app.

The toggle handler in `frontend/pages/reversal_panel` called
`pro_model_fit(force=True)` synchronously. That is the same five-second
RandomForest train that
`test_pro_model_fit_does_not_block_the_loop.py` took off the event loop on the
scoring and per-signal paths -- and it runs on the asyncio loop here too, so
flipping this toggle froze the UI, the EA socket reader and the monitor loop
together.

It was left inline in that change on the reasoning that it is "rare and
user-initiated, where a wait is expected". That is wrong about who waits: a
NiceGUI handler runs on the shared event loop, so the wait is not the toggling
user's alone. The EA reconnects after ten seconds of Python silence.

The refit still happens, and `force=True` is still honoured -- it is simply
started rather than awaited.

**The toggle itself was removed on 2026-09-11** (owner: the feature is no
longer used). The controller's non-blocking fit stays, because pro_model is
still fitted on the signal-capture path, and the rule this file exists for
is unchanged: the five-second train must never be called from anything
running on the event loop. The panel half of the test now checks the toggle
is really gone rather than checking how it behaves.
"""
from __future__ import annotations

import inspect

from backend.src.controllers import engines_controller


class TestTheControllerOffersANonBlockingFit:
    def test_it_exists(self):
        assert hasattr(engines_controller, "pro_model_fit_in_background")

    def test_it_delegates_to_the_services_background_fit(self, monkeypatch):
        from backend.src.services.reversal_engine import pro_model as pm
        seen = {}
        monkeypatch.setattr(pm, "fit_in_background",
                            lambda force=False: seen.update(force=force))

        engines_controller.pro_model_fit_in_background(force=True)

        assert seen == {"force": True}

    def test_the_blocking_one_is_still_available(self):
        """Nothing else should lose the ability to fit synchronously; the
        point is only that the UI stops doing it."""
        assert hasattr(engines_controller, "pro_model_fit")


class TestThePanelNoLongerOffersTheToggle:
    """Removed 2026-09-11 at the owner's request: "Learn from pro signals"
    is no longer used, so `re_learn_from_ref_signals` stays 0 and
    `pro_likeness` stays at its neutral for every signal.

    Pinned rather than deleted so the removal is deliberate and visible. If
    the toggle ever comes back it must come back through
    `pro_model_fit_in_background`, which is what the class above protects."""

    @staticmethod
    def _panel_source() -> str:
        from frontend.pages import reversal_panel

        return inspect.getsource(reversal_panel)

    def test_the_toggle_is_gone(self):
        src = self._panel_source()
        assert "_toggle_learn" not in src
        assert "Learn From Pro Signals" not in src

    def test_the_panel_never_calls_the_blocking_fit(self):
        """The rule that survives the removal. A five-second RandomForest
        train on a NiceGUI handler freezes the UI, the EA socket reader and
        the monitor loop together; the EA reconnects after ten seconds of
        Python silence."""
        body = "\n".join(l for l in self._panel_source().splitlines()
                          if not l.strip().startswith("#"))
        assert "pro_model_fit(" not in body
