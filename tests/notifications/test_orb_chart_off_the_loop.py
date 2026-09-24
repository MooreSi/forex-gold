"""The ORB chart is drawn off the event loop, and without pyplot.

Measured on the owner's Mac, 2026-09-23 08:15:03: `_email_scheduler_loop`
held the event loop for 1.8 s -- the moment the London Open ORB report is
built, and the chart is matplotlib rendering a 9x5 inch, 150 dpi PNG. The
dashboard's ORB panel drew the same chart the same way, inside its request
handler.

Moving it to a worker thread is only safe if the drawing itself is. pyplot is
not: it keeps one global registry of open figures, and the 08:15 email and a
dashboard refresh can draw at the same moment. So the chart now uses
matplotlib's object API -- a `Figure` with its own Agg canvas, nothing global
-- and the first test checks it by making pyplot unimportable.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime
from unittest import mock

from backend.src.services.notifications import email_service


def _on_the_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _report():
    start = 1_758_610_800  # a weekday 07:00 UTC
    candles = [{"ts": start + i * 300, "open": 2400 + i * 0.1, "high": 2401 + i * 0.1,
                "low": 2399 + i * 0.1, "close": 2400.5 + i * 0.1} for i in range(24)]
    return {"candles": candles, "or_start": start, "or_end": start + 3600,
            "or_high": 2402.0, "or_low": 2399.0, "or_range": 3.0,
            "asia_high": 2405.0, "asia_low": 2395.0,
            "direction": "bullish", "target": 2408.0, "stop": 2398.0, "rr": 2.0}


class _NoPyplot:
    def __getattr__(self, name):
        raise AssertionError(f"pyplot.{name} used -- not thread-safe")


def test_the_chart_renders_without_pyplot(monkeypatch):
    monkeypatch.setitem(sys.modules, "matplotlib.pyplot", _NoPyplot())

    png = email_service.build_orb_chart_image(_report())

    assert png and png[:8] == b"\x89PNG\r\n\x1a\n"


def test_two_charts_drawn_at_once_both_come_out_whole():
    async def _both():
        return await asyncio.gather(
            asyncio.to_thread(email_service.build_orb_chart_image, _report()),
            asyncio.to_thread(email_service.build_orb_chart_image, _report()),
        )

    first, second = asyncio.run(_both())

    assert first[:8] == second[:8] == b"\x89PNG\r\n\x1a\n"
    assert first == second


# ── The 08:15 email ──────────────────────────────────────────────────────────

def test_the_scheduled_report_draws_its_chart_off_the_loop(fresh_db):
    from backend.src.db import database as db
    from backend.src.runtime import TradingRuntime
    from backend.src.services.notifications import scheduler as core_email_scheduler

    db.save_email_config({"smtp_host": "smtp.example.com", "orb_report_enabled": 1})
    engine = TradingRuntime.__new__(TradingRuntime)
    engine._monitor_running = True
    engine._cfg = {"x": 1}
    engine._bridge = mock.Mock()
    seen = {}
    sleeps = {"n": 0}

    async def _sleep(*a, **k):
        sleeps["n"] += 1
        if sleeps["n"] >= 2:
            engine._monitor_running = False

    async def _build(bridge):
        return {"ok": True}

    def _chart(report):
        seen["on_loop"] = _on_the_event_loop()
        return b"PNG"

    async def _send(subject, html, cfg, image_bytes=None, image_cid=None):
        seen["image"] = image_bytes
        return True, None

    patcher = mock.patch("backend.src.services.notifications.scheduler.datetime")
    fake_dt = patcher.start()
    fake_dt.now.return_value = datetime(2026, 7, 20, 8, 15, 0)  # a Monday
    fake_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_sleep)), \
             mock.patch.object(core_email_scheduler, "build_orb_report", _build), \
             mock.patch.object(email_service, "build_orb_chart_image", _chart), \
             mock.patch.object(email_service, "build_orb_html", return_value="<html/>"), \
             mock.patch.object(email_service, "send_email", side_effect=_send):
            asyncio.run(engine._email_scheduler_loop())
    finally:
        patcher.stop()

    assert seen["on_loop"] is False
    assert seen["image"] == b"PNG"
