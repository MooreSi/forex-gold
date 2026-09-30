"""Today's Goal must total what the Calendar totals for today (+$2.51 case)."""
from datetime import date

from backend.src.services.analytics import todays_realised as tr

DAY = date(2026, 9, 30)
# 2026-09-30 12:00 UTC stored as broker time (UTC+3) is 15:00 UTC-as-epoch.
NOON = 1_790_769_600.0 + 3 * 3600


def _ts(day_offset=0, hour_shift=0):
    return NOON + day_offset * 86400 + hour_shift * 3600


def _base():
    """Sanity: NOON minus the broker offset is 2026-09-30 in UTC."""
    from backend.src.services.analytics import formatting as f
    assert f.to_date(NOON - f.BROKER_OFFSET) == DAY


def test_sums_only_todays_rows():
    _base()
    table = {"error": None, "rows": [
        {"close_ts": _ts(), "pnl": 4.0},
        {"close_ts": _ts(), "pnl": -1.49},
        {"close_ts": _ts(-1), "pnl": 50.0},
    ]}
    assert tr.realised_on(table, DAY) == 2.51


def test_a_close_before_the_days_first_second_belongs_to_the_day_before():
    """Filed as the Calendar files it: stamp minus UTC+3, read as a UTC date."""
    _base()
    utc_midnight_on_the_day = NOON - 3 * 3600 - 12 * 3600
    stamp = utc_midnight_on_the_day + 3 * 3600 - 1800  # 23:30 the day before
    table = {"error": None, "rows": [{"close_ts": stamp, "pnl": 9.0}]}
    assert tr.realised_on(table, DAY) == 0.0


def test_none_when_mt5_could_not_answer():
    assert tr.realised_on({"error": "down", "rows": []}, DAY) is None
    assert tr.realised_on({}, DAY) is None
