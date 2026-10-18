"""Eligibility is checked after asynchronous review, without any order calls."""
import asyncio
from types import SimpleNamespace
import pytest
from backend.src.services.breakout_signal import entry_safety as e


@pytest.mark.parametrize("age,allowed,spread,refused", [
    (2, True, .2, False), (121, True, .2, True),
    (2, False, .2, True), (2, True, 5, True)])
def test_delays_permissions_and_costs_are_rechecked(monkeypatch, age, allowed, spread, refused):
    from backend.src.services.breakout_signal import adaptive_params, breakout_signal_repo
    from backend.src.services.cluster import node_roles
    from backend.src.db import database
    monkeypatch.setattr(node_roles, "engines_generate_here", lambda: allowed)
    monkeypatch.setattr(database, "is_session_allowed", lambda: (True, "london"))
    async def inline(fn, *args):
        return fn(*args)
    monkeypatch.setattr(database, "to_db_thread", inline)
    monkeypatch.setattr(adaptive_params, "get", lambda key: 2)
    monkeypatch.setattr(breakout_signal_repo, "log_analysis", lambda row: None)
    tick = SimpleNamespace(bid=100, ask=100 + spread)
    assert asyncio.run(e.final_refused(tick, {"ts": 100 - age}, False, now=100)) is refused


def test_receiving_old_bars_now_does_not_make_them_fresh(monkeypatch):
    from backend.src.services.breakout_signal import adaptive_params, breakout_signal_repo
    async def allowed():
        return True
    monkeypatch.setattr(e, "generation_permitted", allowed)
    monkeypatch.setattr(e, "quote_problem", lambda *a: None)
    monkeypatch.setattr(breakout_signal_repo, "log_analysis", lambda row: None)
    monkeypatch.setattr(adaptive_params, "get", lambda key: 2)
    tick = SimpleNamespace(timestamp=100, bid=100, ask=100.2)
    assert asyncio.run(e.final_refused(tick, {"ts": 99, "source_closed_at": -201}, False, now=100))
    assert not asyncio.run(e.final_refused(tick, {"ts": 99, "source_closed_at": 99}, False, now=100))
