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


class TestThePanelUsesIt:
    @staticmethod
    def _toggle_source() -> str:
        from frontend.pages import reversal_panel

        src = inspect.getsource(reversal_panel)
        start = src.index("def _toggle_learn")
        return src[start:start + 1200]

    def test_the_toggle_does_not_call_the_blocking_fit(self):
        body = "\n".join(l for l in self._toggle_source().splitlines()
                         if not l.strip().startswith("#"))

        assert "pro_model_fit(" not in body, (
            "the toggle still runs the ~5s train on the event loop"
        )

    def test_the_toggle_starts_a_background_fit(self):
        assert "pro_model_fit_in_background" in self._toggle_source()
