"""Momentum-exhaustion / rejection re-check -- run at fill time by both
Breakout Engine and Reversal Engine to catch a stale signal firing into a
market that already made (or reversed) its move. Pure function, no DB.
"""
from forex_trader.core.momentum_exhaustion import check_momentum_exhaustion

ATR = 8.0


def _candle(o, h, l, c):
    return {"open": o, "high": h, "low": l, "close": c}


def test_no_candles_passes():
    ok, reason = check_momentum_exhaustion("SELL", [], ATR)
    assert ok
    assert reason == ""


def test_zero_atr_passes():
    c = [_candle(2400, 2401, 2380, 2381)]
    ok, _ = check_momentum_exhaustion("SELL", c, 0.0)
    assert ok


def test_zero_range_candle_passes():
    c = [_candle(2400, 2400, 2400, 2400)]
    ok, _ = check_momentum_exhaustion("SELL", c, ATR)
    assert ok


def test_calm_candle_passes_both_directions():
    c = [_candle(2395.0, 2396.0, 2393.5, 2394.5)]
    ok_sell, _ = check_momentum_exhaustion("SELL", c, ATR)
    ok_buy, _  = check_momentum_exhaustion("BUY", c, ATR)
    assert ok_sell
    assert ok_buy


def test_sell_blocked_by_already_exhausted_down_move():
    # Large down candle closing near its low -- the down move already
    # happened; a fresh SELL here would be chasing it.
    c = [_candle(2400.0, 2401.0, 2380.0, 2381.0)]
    ok, reason = check_momentum_exhaustion("SELL", c, ATR)
    assert not ok
    assert "exhaustion" in reason


def test_buy_blocked_by_already_exhausted_up_move():
    c = [_candle(2380.0, 2401.0, 2379.5, 2400.0)]
    ok, reason = check_momentum_exhaustion("BUY", c, ATR)
    assert not ok
    assert "exhaustion" in reason


def test_sell_blocked_by_reversal_against_direction():
    # Strong up candle while we'd be selling -- market has turned.
    c = [_candle(2380.0, 2401.0, 2379.5, 2400.0)]
    ok, reason = check_momentum_exhaustion("SELL", c, ATR)
    assert not ok
    assert "reversal" in reason


def test_buy_blocked_by_reversal_against_direction():
    c = [_candle(2400.0, 2401.0, 2380.0, 2381.0)]
    ok, reason = check_momentum_exhaustion("BUY", c, ATR)
    assert not ok
    assert "reversal" in reason


def test_sell_blocked_by_hammer_rejection_at_lows():
    # Small net body, but a long lower wick -- buyers defended the lows.
    c = [_candle(2395.0, 2396.0, 2380.0, 2394.0)]
    ok, reason = check_momentum_exhaustion("SELL", c, ATR)
    assert not ok
    assert "rejection" in reason


def test_buy_blocked_by_shooting_star_rejection_at_highs():
    c = [_candle(2395.0, 2410.0, 2394.0, 2396.0)]
    ok, reason = check_momentum_exhaustion("BUY", c, ATR)
    assert not ok
    assert "rejection" in reason


def test_direction_is_case_insensitive():
    c = [_candle(2400.0, 2401.0, 2380.0, 2381.0)]
    ok, _ = check_momentum_exhaustion("sell", c, ATR)
    assert not ok


def test_missing_ohlc_keys_pass_safely():
    ok, reason = check_momentum_exhaustion("SELL", [{"close": 100}], ATR)
    assert ok
    assert reason == ""
