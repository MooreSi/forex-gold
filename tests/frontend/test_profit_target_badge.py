"""The header badge reports the daily profit target, and clicking it resumes.

**Why this exists.** The daily profit target (Trading > Schedule) refuses every
automated entry for the rest of the day once the day's realised P&L clears it.
The header said "Circuit Breaker OK" throughout -- true about the breaker, and
a false all-clear about whether orders were being placed. That is the same
defect the news blackout box was added to fix on 2026-09-04, for a gate that
lasts hours rather than minutes.

The state itself is decided in the service and tested there
(tests/risk/test_daily_target_resume.py), following news_pause_state. What is
left to prove is the wiring: that the shell renders that state, and that the
badge's own dialog clears it. A badge wired to nothing renders perfectly.

The state reader is stubbed rather than driven through the database because the
render harness points at a temp database with no trades in it, and inserting a
day's closed trades to move one label proves less about the header than it
costs to read.
"""
from __future__ import annotations

import pytest

from backend.src.controllers import schedule_controller as schedule_ctl


def _state(**over):
    state = {"reached": False, "overridden": False, "pnl": 0.0, "target": 0.0}
    state.update(over)
    return state


@pytest.fixture
def target_reached(monkeypatch):
    """Both readers the shell uses -- the async one for the 5s badge poll, the
    sync one for the click handler that opens the dialog."""
    reached = _state(reached=True, pnl=120.0, target=100.0)

    async def _async():
        return reached

    monkeypatch.setattr(schedule_ctl, "daily_profit_target_state_async", _async)
    monkeypatch.setattr(schedule_ctl, "daily_profit_target_state", lambda *a, **k: reached)
    return reached


@pytest.mark.asyncio
async def test_the_badge_says_the_target_is_reached(user, target_reached):
    await user.open("/")
    await user.should_see("Profit Target Reached")


@pytest.mark.asyncio
async def test_the_badge_shows_the_all_clear_when_it_is_not(user, monkeypatch):
    async def _async():
        return _state()

    monkeypatch.setattr(schedule_ctl, "daily_profit_target_state_async", _async)
    await user.open("/")
    await user.should_see("Circuit Breaker OK")


@pytest.mark.asyncio
async def test_a_resumed_target_is_not_announced(user, monkeypatch):
    """Once resumed, orders are being placed again -- the badge has to go back
    to the all-clear or the operator resumes a halt that is already lifted."""
    async def _async():
        return _state(overridden=True, pnl=120.0, target=100.0)

    monkeypatch.setattr(schedule_ctl, "daily_profit_target_state_async", _async)
    await user.open("/")
    await user.should_see("Circuit Breaker OK")


@pytest.mark.asyncio
async def test_clicking_the_badge_offers_to_resume_with_both_figures(user, target_reached):
    await user.open("/")
    await user.should_see("Profit Target Reached")
    # The click handler is on the row, not the label inside it. A real browser
    # bubbles; the user simulation does not, so the row carries a marker.
    user.find(marker="trading-status-badge").click()

    await user.should_see("Re-enable trading?")
    await user.should_see("$120.00 of $100.00")


@pytest.mark.asyncio
async def test_confirming_resumes_past_the_target(user, target_reached, monkeypatch):
    calls = []
    monkeypatch.setattr(
        schedule_ctl, "resume_past_daily_profit_target",
        lambda *a, **k: calls.append(True),
    )

    await user.open("/")
    await user.should_see("Profit Target Reached")
    user.find(marker="trading-status-badge").click()
    await user.should_see("Resume Trading")
    user.find(marker="resume-confirm-button").click()

    await user.should_see("Trading resumed")
    assert calls, "the Resume button did not lift the daily target"
