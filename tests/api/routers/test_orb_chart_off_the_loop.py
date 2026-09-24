"""The dashboard's ORB panel draws its chart off the event loop.

The same matplotlib render the 08:15 email makes (1.8 s on the loop,
2026-09-23), inside the panel's request handler. See
tests/notifications/test_orb_chart_off_the_loop.py for why it is also drawn
without pyplot.
"""
from __future__ import annotations

import asyncio


def _on_the_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


# ── The dashboard's ORB panel ────────────────────────────────────────────────

def test_the_dashboard_draws_its_chart_off_the_loop(make_client, monkeypatch):
    from backend.src.api.routers import orb as orb_router
    seen = {}

    async def _build():
        return {"direction": "inside"}

    def _chart(report):
        seen["on_loop"] = _on_the_event_loop()
        return b"PNG"

    monkeypatch.setattr(orb_router.notify_ctl, "build_orb_report", _build)
    monkeypatch.setattr(orb_router.notify_ctl, "build_orb_chart_image", _chart)
    monkeypatch.setattr(orb_router.trading_ctl, "get_risk_settings", lambda: {})
    monkeypatch.setattr(orb_router.engines_ctl, "control_target", lambda: "local")

    assert make_client().get("/api/trading/orb").status_code == 200
    assert seen["on_loop"] is False
