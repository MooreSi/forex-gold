"""The slow synchronous reads behind two dashboard routes run off the event
loop (bugs/030, after the React port).

The Mac's log, 2026-10-05: the loop that dispatches orders, watches positions
and sends alerts stalled 4,585 ms inside `reversal_report` (shadow_repo's
closed_decisions, a full read of the virtual ledger) and 956 ms inside
`/api/ai/evidence` (ai_analysis_repo._gather_channel_data). Both were plain
function calls from an async handler. They now go through asyncio.to_thread;
the adapters they use are locked for cross-thread use (sqlite_adapter.py) or
open their own connection by path.

A function running on the loop's own thread sees a running loop; one in a
worker thread does not. That is what is recorded.
"""
from __future__ import annotations

import asyncio

from backend.src.api.routers import ai as ai_router
from backend.src.api.routers import engines as engines_router

from tests.api.routers.test_engines import engines  # noqa: F401
from tests.api.routers.test_ai import lab  # noqa: F401


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def test_the_reversal_report_reads_its_ledgers_off_the_loop(make_client, engines, monkeypatch):
    seen = {}

    async def _edge_stats():
        return {}
    monkeypatch.setattr(engines_router.reversal_ctl, "reversal_edge_stats", _edge_stats)
    monkeypatch.setattr(engines_router.reversal_ctl, "reversal_shadow_report",
                        lambda: seen.setdefault("shadow", _on_loop()) and [] or [])
    monkeypatch.setattr(engines_router.reversal_ctl, "reversal_shadow_history",
                        lambda limit: seen.setdefault("history", _on_loop()) and [] or [])
    monkeypatch.setattr(engines_router.reversal_ctl, "reversal_shadow_ledger",
                        lambda limit: seen.setdefault("ledger", _on_loop()) and [] or [])

    assert make_client().get("/api/engines/reversal/report").status_code == 200
    assert seen == {"shadow": False, "history": False, "ledger": False}


def test_the_ai_evidence_is_gathered_off_the_loop(make_client, lab, monkeypatch):
    seen = []
    monkeypatch.setattr(ai_router.ai_analysis_ctl, "gather_channel_data",
                        lambda path, days: seen.append(_on_loop()) or {"rows": []})
    assert make_client().get("/api/ai/evidence?subject=channels&days=30").status_code == 200
    assert seen == [False]
