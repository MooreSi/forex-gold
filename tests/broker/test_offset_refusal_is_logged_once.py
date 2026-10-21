"""A tick-range read with no trustworthy server-time offset must say so once,
not on every call.

2026-10-21, reported live on a Windows client: the bridge logged "no
trustworthy server-time offset yet" 401 times in about two hours, the
evidence collector asking every 30s. The line is true each time and says
nothing new, and it pushed the log's real warnings out of the 500-line
diagnostics tail the admin console shows.
"""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import mt5_ticks

NOW = 1_790_000_000.0


class _ClosedMarketTerminal:
    COPY_TICKS_ALL = -1

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(time=int(NOW - 47 * 60))   # 47 minutes stale

    def copy_ticks_range(self, *args):
        raise AssertionError("must not be reached without an offset")


@pytest.fixture(autouse=True)
def _clean():
    mt5_ticks.reset_offset_cache()
    yield
    mt5_ticks.reset_offset_cache()


def _refusals(caplog):
    return [r for r in caplog.records if "trustworthy server-time offset" in r.getMessage()]


def test_repeated_refusals_log_once_then_again_after_the_interval(monkeypatch, caplog):
    now = [NOW]
    monkeypatch.setattr(mt5_ticks.time, "time", lambda: now[0])
    with caplog.at_level(logging.WARNING, logger="mt5_bridge"):
        for _ in range(20):
            assert mt5_ticks.read_range(_ClosedMarketTerminal(), "XAUUSD", NOW - 60, NOW,
                                        lambda: True, 86_400) is None
            now[0] += 30
        assert len(_refusals(caplog)) == 1

        now[0] += mt5_ticks._REFUSAL_LOG_EVERY_S
        mt5_ticks.read_range(_ClosedMarketTerminal(), "XAUUSD", NOW - 60, NOW,
                             lambda: True, 86_400)
        assert len(_refusals(caplog)) == 2
