"""A retest needs an observed break event, never just a continuation candle."""
import pytest
from backend.src.services.breakout_signal import signal_generator as sg


def candle(o, c, low=None):
    return {"open": o, "high": max(o, c) + .2, "low": min(o, c) - .2 if low is None else low, "close": c}


@pytest.fixture(autouse=True)
def params(monkeypatch):
    monkeypatch.setattr(sg.ap, "get", lambda key: sg.ap.PARAMS[key]["default"])


def retest(bars):
    return sg.check_breakout_retest(bars, [{"price": 100, "type": "resistance", "strength": 2}],
                                  "bullish", "bullish", 101, 10, 30, 1)


def test_continuation_is_not_a_new_break():
    bars = [candle(105, 106, 104) for _ in range(7)] + [candle(104, 110), candle(100.1, 101, 99.8)]
    assert retest(bars) is None


def test_fresh_cross_then_first_retest_passes():
    bars = [candle(98, 98) for _ in range(6)] + [candle(99, 106), candle(106, 106, 105), candle(100.1, 101, 99.8)]
    assert retest(bars)["breakout_type"] == "retest"


def test_retest_retains_its_origin_and_consumed_touches():
    bars = [{**c, "ts": i * 300} for i, c in enumerate(
        [candle(98, 98) for _ in range(6)] +
        [candle(99, 106), candle(106, 106, 105), candle(100.1, 101, 99.8)])]
    event = retest(bars)["break_event"]
    assert event["origin_ts"] == 1800
    assert event["prior_touches"] == 0
    assert event["level"] == 100


def test_continuation_cannot_erase_a_consumed_touch():
    bars = [candle(98, 98) for _ in range(4)] + [candle(99, 106), candle(106, 106, 102),
            candle(105, 111, 105), candle(100.1, 101, 99.8)]
    assert retest(bars) is None
