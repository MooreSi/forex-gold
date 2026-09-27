"""The app watchdog must not restart an engine the other node stood down.

On the VPS, 2026-09-26: the Mac sent STAND_DOWN at 14:35:58, 15:21:58, 16:11:42
and 16:17:21, the sync server stopped the breakout engine each time and
recorded it in `sync_stood_down_engines`, and the 5-minute app watchdog
restarted it every time -- 14:38:45, 15:26:53, 16:11:54, 16:22:08. The
watchdog only asked "enabled but not running?", and a stood-down engine is
exactly that. At RESUME the server then found nothing to restart
("restarted engines []") because the watchdog had already done it.

open_trade's own gate still blocked the trades, so no order went through.
But a stood-down engine should be down: it was scanning, scoring and
notifying on a node that had handed over.
"""
from __future__ import annotations

import pytest

from backend.src import app
from backend.src.db import database as db_module
from backend.src.services.breakout_signal import breakout_signal_repo
from backend.src.services.reversal_engine import reversal_engine_repo


class _Engine:
    def __init__(self):
        self.is_running = False
        self.starts = 0

    def start(self):
        self.starts += 1
        self.is_running = True


@pytest.fixture
def engines(monkeypatch):
    bo, re_ = _Engine(), _Engine()
    monkeypatch.setattr(app._breakout_engine_module, "get_instance", lambda: bo)
    monkeypatch.setattr(app._re_engine_module, "get_instance", lambda: re_)
    monkeypatch.setattr(breakout_signal_repo, "get_config",
                        lambda key, default=None: "1" if key == "bo_engine_enabled" else default)
    monkeypatch.setattr(reversal_engine_repo, "get_config",
                        lambda key, default=None: "0" if key == "re_user_stopped" else default)
    return bo, re_


def test_an_engine_that_was_stood_down_stays_down(engines, monkeypatch):
    bo, re_ = engines
    monkeypatch.setattr(db_module, "get_stood_down_engines",
                        lambda: ["breakout", "reversal_engine"])

    app._signal_engine_watchdog_pass()

    assert bo.starts == 0
    assert re_.starts == 0


def test_an_engine_that_merely_stopped_is_still_restarted(engines, monkeypatch):
    bo, re_ = engines
    monkeypatch.setattr(db_module, "get_stood_down_engines", lambda: [])

    app._signal_engine_watchdog_pass()

    assert bo.starts == 1
    assert re_.starts == 1


def test_standing_down_one_engine_does_not_hold_back_the_other(engines, monkeypatch):
    bo, re_ = engines
    monkeypatch.setattr(db_module, "get_stood_down_engines", lambda: ["breakout"])

    app._signal_engine_watchdog_pass()

    assert bo.starts == 0
    assert re_.starts == 1
