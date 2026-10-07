"""Completed-session levels from local candles; no broker is reachable."""
from datetime import datetime, timezone
from backend.src.services.reversal_engine import level_detector as ld


def candles(day, low, high, hours=range(9)):
    return [{"ts": datetime(2026, 10, day, h, tzinfo=timezone.utc).timestamp(),
             "low": low, "high": high} for h in hours]


def test_asia_range_uses_latest_completed_session_only():
    assert ld.get_asia_range(candles(5, 100, 110) + candles(6, 200, 210)) == (200, 210)


def test_asia_range_excludes_current_incomplete_session():
    assert ld.get_asia_range(candles(5, 100, 110) + candles(6, 200, 210, range(4))) == (100, 110)


def test_partial_history_does_not_claim_a_complete_asia_range():
    assert ld.get_asia_range(candles(6, 200, 210, range(4, 9))) == (0, 0)


def test_out_of_order_input_does_not_change_latest_session():
    assert ld.get_asia_range(list(reversed(candles(5, 100, 110)+candles(6, 200, 210)))) == (200, 210)
