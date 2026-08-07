"""The forex trading week (dpm_engine.is_weekly_market_closed) and the suite's
stub of it.

Two reasons this file exists.

The function had no tests at all, despite gating every automated entry in the
app: core_db_risk_settings.is_session_allowed returns ("closed") from it, and
core_signal_resolution.resolve raises on that before anything else runs. A
wrong boundary here silently stops all trading, or silently allows it into a
dead market.

And as of 2026-08-07 tests/conftest.py pins the week open for the whole suite,
because 147 tests that had nothing to do with market hours failed the moment
the real week closed. A global stub with nothing checking the real function
would mean a broken boundary went unnoticed, so the tests below take the
`live_market_hours` marker to opt back out -- which is also the only way to
reach the real function at all now.

No MT5 order is ever placed, closed, or modified by any of this.
"""
from datetime import datetime, timezone

import pytest

from forex_trader.core import dpm_engine


def _utc(year, month, day, hour, minute=0):
    return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)


# 2026-08-03 is a Monday, so this week runs Mon 3rd to Sun 9th August.
MONDAY, FRIDAY, SATURDAY, SUNDAY = 3, 7, 8, 9


@pytest.mark.live_market_hours
@pytest.mark.parametrize("when, closed, why", [
    (_utc(2026, 8, MONDAY,   9), False, "Monday morning, London open"),
    (_utc(2026, 8, FRIDAY,  20), False, "Friday, an hour before the close"),
    (_utc(2026, 8, FRIDAY,  21), True,  "Friday 21:00 UTC is the close itself"),
    (_utc(2026, 8, FRIDAY,  23), True,  "Friday night, shut"),
    (_utc(2026, 8, SATURDAY, 0), True,  "Saturday is shut all day"),
    (_utc(2026, 8, SATURDAY, 23), True, "still Saturday, still shut"),
    (_utc(2026, 8, SUNDAY,  21), True,  "Sunday, an hour before the reopen"),
    (_utc(2026, 8, SUNDAY,  22), False, "Sunday 22:00 UTC is the reopen"),
])
def test_week_boundaries(when, closed, why):
    assert dpm_engine.is_weekly_market_closed(when) is closed, why


@pytest.mark.live_market_hours
def test_the_close_is_inclusive_and_the_reopen_is_immediate():
    """The two edges in isolation, since an off-by-one hour either way is
    invisible in aggregate but means an hour of trading into a dead market or
    an hour of refusing signals after it reopened."""
    assert dpm_engine.is_weekly_market_closed(_utc(2026, 8, FRIDAY, 20, 59)) is False
    assert dpm_engine.is_weekly_market_closed(_utc(2026, 8, FRIDAY, 21, 0)) is True

    assert dpm_engine.is_weekly_market_closed(_utc(2026, 8, SUNDAY, 21, 59)) is True
    assert dpm_engine.is_weekly_market_closed(_utc(2026, 8, SUNDAY, 22, 0)) is False


def test_the_suite_stub_holds_the_week_open():
    """No marker, so conftest's stub applies. This is what stops the suite
    meaning something different on a Saturday than it does on a Tuesday --
    if it ever fails, 147 tests are about to start failing with it."""
    assert dpm_engine.is_weekly_market_closed(_utc(2026, 8, SATURDAY, 12)) is False
    assert dpm_engine.is_weekly_market_closed() is False


def test_the_stub_reaches_the_gate_the_147_failed_on():
    """Patching dpm_engine alone is not obviously enough -- is_session_allowed
    imports the function inside its own body. This is the assertion that the
    stub actually lands where it matters."""
    from forex_trader.core import core_db_risk_settings

    # Reaching detect_session at all means the weekly-close branch let it past;
    # a real closed week would have short-circuited to "closed" first.
    _allowed, session = core_db_risk_settings.is_session_allowed(
        {"session_asia_enabled": 1, "session_london_enabled": 1, "session_ny_enabled": 1}
    )

    assert session != "closed"
