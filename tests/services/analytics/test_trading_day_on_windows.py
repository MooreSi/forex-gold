"""A close the trading clock cannot place has no trading day, on every platform.

Main's CI failed on three `test_trade_table.py` tests on every run from at
least 2026-10-16 to 2026-10-17, on the Windows runner only, with
`OSError: [Errno 22] Invalid argument`. Those tests stamp closes seconds
after 1970; `broker_ts_to_local_date` takes the broker's three hours off, and
CPython on Windows refuses a negative epoch in `fromtimestamp` (and in the
local-zone `astimezone` after it) where macOS and Linux return 1969. Its
docstring already promised None "on anything unparseable"; it caught only
TypeError and ValueError. `trade_table._session` beside it already
caught OSError.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from backend.src.services.analytics import formatting
from backend.src.utils import trading_clock


class _WindowsDatetime(datetime):
    @classmethod
    def fromtimestamp(cls, t, tz=None):
        if float(t) < 0:
            raise OSError(22, "Invalid argument")
        return super().fromtimestamp(t, tz)


def test_a_close_windows_cannot_convert_has_no_day(monkeypatch):
    monkeypatch.setattr(trading_clock, "datetime", _WindowsDatetime)
    assert formatting.broker_ts_to_local_date(1.0, 0) is None


def test_the_local_zone_refusing_it_has_no_day_either(monkeypatch):
    """With no offset configured the machine's zone is asked, and Windows
    refuses a pre-1970 instant there too."""
    def _refuse(epoch, offset_minutes=None):
        raise OSError(22, "Invalid argument")
    monkeypatch.setattr(formatting, "local_from_timestamp", _refuse)
    assert formatting.broker_ts_to_local_date(1.0, None) is None


def test_a_real_close_still_gets_its_day(monkeypatch):
    monkeypatch.setattr(trading_clock, "datetime", _WindowsDatetime)
    # 2026-10-16 23:30 broker time is 20:30 UTC, the 16th on a UTC clock.
    ts = datetime(2026, 10, 16, 23, 30, tzinfo=timezone.utc).timestamp()
    assert formatting.broker_ts_to_local_date(ts, 0) == date(2026, 10, 16)
