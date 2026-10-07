"""One definition of the trading sessions, used everywhere (bugs/057).

Six copies of "which session is this hour" disagreed on eight hours of the
day: the Trading Markets gate and P&L attribution called 21:00-06:59 Asian,
the Reversal Engine 00:00-07:59, the Breakout engine 23:00-07:59, the
strategy AI 00:00-06:59. Owner, 2026-10-07, chose:

    asian    00:00-06:59
    london   07:00-11:59   (London's 08:00 local open is 07:00 UTC in summer)
    overlap  12:00-15:59
    ny       16:00-20:59
    off      21:00-23:59   no session: the gate refuses automated trading

Measured before choosing: 29 real trades since 20 Aug opened 21:00-23:59
UTC, net -$174 (the Reversal Engine won 11 of 28).
"""
from datetime import datetime, timezone
from unittest import mock

import pytest

from backend.src.utils import sessions as S

EXPECTED = (["asian"] * 7 + ["london"] * 5 + ["overlap"] * 4 + ["ny"] * 5 + ["off"] * 3)


def test_the_definition():
    assert [S.session_for_hour(h) for h in range(24)] == EXPECTED


def test_the_reversal_engine_uses_it():
    from backend.src.services.reversal_engine import level_detector as ld
    assert [ld.get_session(h) for h in range(24)] == EXPECTED


def test_pnl_attribution_uses_it():
    from backend.src.services.analytics import read_repo
    assert [read_repo._session_for_hour(h) for h in range(24)] == EXPECTED


def _at(hour, weekday_date="2026-10-07"):                     # a Wednesday
    return datetime.fromisoformat(f"{weekday_date}T{hour:02d}:30:00+00:00")


class _Clock:
    """Stands in for `datetime` inside a module that calls datetime.now()."""
    def __init__(self, now):
        self._now = now

    def now(self, tz=None):
        return self._now


def test_the_gate_and_dpm_use_it():
    from backend.src.services.dpm import engine as dpm
    got = []
    for h in range(24):
        with mock.patch.object(dpm, "datetime", _Clock(_at(h))):
            # The real function: tools/testing/fixed_clock pins detect_session
            # for the suite and keeps the original under this name.
            got.append(getattr(dpm, "detect_session_unpinned", dpm.detect_session)())
    assert got == EXPECTED


def test_the_breakout_engine_uses_it_on_a_weekday():
    from backend.src.services.market import sessions as ms
    got = []
    for h in range(24):
        with mock.patch.object(ms, "datetime", _Clock(_at(h))):
            got.append(ms.get_session())
    assert got == EXPECTED


def test_the_breakout_backtest_uses_it():
    from backend.src.services.breakout_signal import backtest as bt
    assert [bt._session_at(_at(h).timestamp()) for h in range(24)] == EXPECTED


def test_the_weekend_is_still_closed():
    from backend.src.services.market import sessions as ms
    with mock.patch.object(ms, "datetime", _Clock(_at(10, "2026-10-10"))):   # Saturday
        assert ms.get_session() == "closed"


def test_the_strategy_ai_boundaries_are_the_same():
    from backend.src.services.channels import strategy_ai as sa
    assert sa._LONDON_START_UTC == S.LONDON_START == 7
    assert sa._ASIAN_END_UTC == S.LONDON_START
    assert sa._OVERLAP_START_UTC == S.OVERLAP_START == 12
    assert sa._OVERLAP_END_UTC == S.NY_START == 16


@pytest.mark.parametrize("hour,asia,london,ny,ok", [
    (22, 1, 1, 1, False),      # no session: refused whatever the buttons say
    (7, 0, 1, 0, True),        # 07:00 is London now
    (6, 1, 0, 0, True),
    (16, 0, 1, 0, False),      # 16:00 is New York only
])
def test_the_trading_markets_gate(hour, asia, london, ny, ok):
    from backend.src.services.dpm import engine as dpm
    from backend.src.services.risk import risk_settings_repo as rr
    rs = {"session_asia_enabled": asia, "session_london_enabled": london,
          "session_ny_enabled": ny}
    real = getattr(dpm, "detect_session_unpinned", dpm.detect_session)
    with mock.patch.object(dpm, "datetime", _Clock(_at(hour))), \
         mock.patch.object(dpm, "detect_session", real), \
         mock.patch.object(dpm, "is_weekly_market_closed", lambda *a: False):
        assert rr.is_session_allowed(rs)[0] is ok
