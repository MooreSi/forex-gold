"""Reversal Engine news blackout: no live fill while a window is open.

The gate sits in _try_live_execute rather than at signal creation, because a
Reversal Engine signal sits pending for up to 2h waiting for price to enter its
zone -- so the window that matters is the one at fill time, not the one when
the level was first spotted. A signal created in the clear and triggering
inside a window must be held; one created inside a window and triggering after
it must not be.

Virtual tracking is deliberately unaffected, so the ML keeps learning from what
the gate skipped. Nothing here places, modifies or closes an MT5 order: the
test asserts that _try_live_execute returns before reaching the order path.
"""
from unittest import mock

import pytest

from backend.src.services.reversal_engine.reversal_engine_live_execute import _LiveExecuteMixin


_BLOCKED = (False, "News blackout — Non-Farm Employment Change (USD), resumes in 40 min")
_CLEAR = (True, "")


class _Engine(_LiveExecuteMixin):
    """Minimal host for the mixin. _main_eng is what actually places the order;
    leaving it None means any attempt to trade past the gate raises rather than
    silently passing the test."""
    def __init__(self):
        self._main_eng = None
        self._bridge = None


@pytest.fixture
def engine():
    return _Engine()


@pytest.fixture
def statuses(monkeypatch):
    """Capture update_live_exec calls instead of writing to the RE database."""
    seen = {}

    def _update(sig_id, status=None, **kw):
        seen[sig_id] = status

    monkeypatch.setattr(
        "backend.src.services.reversal_engine.reversal_engine_live_execute.re_db.update_live_exec",
        _update,
    )
    return seen


def _run(engine, sig_id=1):
    import asyncio
    return asyncio.run(engine._try_live_execute(
        {"id": sig_id, "signal_ref": f"RE-{sig_id}", "direction": "BUY"},
        4050.0,
        None,
    ))


@pytest.fixture
def live_enabled(monkeypatch):
    """Live execution on and the Trading Schedule clear, so the news gate is
    the only thing that can stop the flow."""
    monkeypatch.setattr(
        "backend.src.db.database.get_risk_settings",
        lambda: {"re_live_execution": 1, "strategy_lot_size": 0.01},
    )
    monkeypatch.setattr(
        "backend.src.services.risk.schedule.check_trading_schedule",
        lambda **kw: (True, ""),
    )


def test_open_news_window_skips_live_execution(engine, statuses, live_enabled):
    with mock.patch(
        "backend.src.utils.news_calendar.check_news_blackout", return_value=_BLOCKED,
    ):
        _run(engine)
    assert statuses[1] == "skipped:news"


def test_skip_status_is_distinct_from_the_schedule_skip(engine, statuses, live_enabled):
    """The orphan watchdog and the panel read this status; a signal held for
    news must not be indistinguishable from one held by the schedule."""
    with mock.patch(
        "backend.src.utils.news_calendar.check_news_blackout", return_value=_BLOCKED,
    ):
        _run(engine)
    assert statuses[1] != "skipped:schedule"


def test_clear_window_lets_the_flow_run_past_every_early_gate(engine, statuses, live_enabled):
    """Positive control: with no window open the gate must not fire, or the
    engine would never trade again. Execution gets as far as the fill-time ML
    re-evaluation, which is past the news gate and past the exposure guard --
    it then stops only because this stub has no bridge to read candles from,
    and so records no skip status at all."""
    with mock.patch(
        "backend.src.utils.news_calendar.check_news_blackout", return_value=_CLEAR,
    ):
        _run(engine)
    assert statuses == {}


def test_live_execution_disabled_short_circuits_before_the_news_gate(engine, statuses, monkeypatch):
    """With live execution off there is nothing to protect, and the news gate
    should not be what the signal gets attributed to."""
    monkeypatch.setattr(
        "backend.src.db.database.get_risk_settings",
        lambda: {"re_live_execution": 0},
    )
    with mock.patch(
        "backend.src.utils.news_calendar.check_news_blackout", return_value=_BLOCKED,
    ):
        _run(engine)
    assert statuses[1] == "skipped:live_disabled"
