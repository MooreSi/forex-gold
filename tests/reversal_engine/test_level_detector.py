"""level_detector.py had zero test coverage before tonight's Reversal Engine
improvements. Covers: get_htf_bias()'s H4-confirmation logic (written but
never actually reachable until both call sites started passing h4_candles),
and get_congestion_zones() (documented/scored since inception but never
actually implemented until now).
"""
from forex_trader.reversal_engine import level_detector as ld


def _candle(high, low):
    return {"high": high, "low": low}


def _rising_h1_candles(n=20, start=4000.0, step=1.0):
    """Higher highs + higher lows every candle -- unambiguously bullish H1."""
    return [_candle(start + i * step + 5, start + i * step) for i in range(n)]


def _falling_h1_candles(n=20, start=4100.0, step=1.0):
    """Lower highs + lower lows every candle -- unambiguously bearish H1."""
    return [_candle(start - i * step + 5, start - i * step) for i in range(n)]


def _rising_h4_candles(n=6, start=4000.0, step=4.0):
    return [_candle(start + i * step + 10, start + i * step) for i in range(n)]


def _falling_h4_candles(n=6, start=4100.0, step=4.0):
    return [_candle(start - i * step + 10, start - i * step) for i in range(n)]


# ── Without H4 (unchanged pre-existing behaviour) ────────────────────────────

def test_bullish_h1_with_no_h4_stays_bullish():
    assert ld.get_htf_bias(_rising_h1_candles()) == "bullish"


def test_bearish_h1_with_no_h4_stays_bearish():
    assert ld.get_htf_bias(_falling_h1_candles()) == "bearish"


def test_too_few_candles_is_neutral():
    assert ld.get_htf_bias(_rising_h1_candles(n=5)) == "neutral"


# ── H4 confirmation (the newly-reachable branch) ─────────────────────────────

def test_bullish_h1_confirmed_by_bullish_h4_stays_bullish():
    assert ld.get_htf_bias(_rising_h1_candles(), _rising_h4_candles()) == "bullish"


def test_bullish_h1_not_confirmed_by_falling_h4_downgrades_to_neutral():
    """H1 alone says bullish, but H4 disagrees -- exactly the case the
    module's own docstring describes as the reason this branch exists."""
    assert ld.get_htf_bias(_rising_h1_candles(), _falling_h4_candles()) == "neutral"


def test_bearish_h1_confirmed_by_bearish_h4_stays_bearish():
    assert ld.get_htf_bias(_falling_h1_candles(), _falling_h4_candles()) == "bearish"


def test_bearish_h1_not_confirmed_by_rising_h4_downgrades_to_neutral():
    assert ld.get_htf_bias(_falling_h1_candles(), _rising_h4_candles()) == "neutral"


def test_too_few_h4_candles_is_ignored_not_erroring():
    """h4_candles present but under the 6-bar minimum get_htf_bias actually
    reads -- must fall back to H1-only, not raise."""
    assert ld.get_htf_bias(_rising_h1_candles(), _rising_h4_candles(n=3)) == "bullish"


def test_empty_h4_candles_list_is_ignored():
    assert ld.get_htf_bias(_rising_h1_candles(), []) == "bullish"


# ── get_congestion_zones() ───────────────────────────────────────────────────

def _tight_window():
    """5 overlapping candles: window_range=3 vs avg_single_range=1.7*2.5=4.25
    -- inside the threshold, so this is a congestion zone. Midpoint (4003+4000)/2
    = 4001.5."""
    return [
        _candle(4002.0, 4000.0),
        _candle(4003.0, 4001.0),
        _candle(4001.5, 4000.5),
        _candle(4002.5, 4000.8),
        _candle(4002.0, 4000.2),
    ]


def _trending_window():
    """5 candles marching steadily higher, each with a range of 5:
    window_range=25 vs avg_single_range=5*2.5=12.5 -- outside the threshold,
    not congestion."""
    return [
        _candle(4005.0, 4000.0),
        _candle(4010.0, 4005.0),
        _candle(4015.0, 4010.0),
        _candle(4020.0, 4015.0),
        _candle(4025.0, 4020.0),
    ]


def test_tight_window_is_detected_as_congestion_with_correct_midpoint():
    zones = ld.get_congestion_zones(_tight_window())
    assert len(zones) == 1
    assert zones[0]["type"] == "congestion"
    assert zones[0]["price"] == 4001.5


def test_trending_window_is_not_flagged_as_congestion():
    assert ld.get_congestion_zones(_trending_window()) == []


def test_too_few_candles_returns_empty_list_without_erroring():
    assert ld.get_congestion_zones(_tight_window()[:3]) == []


def test_zero_high_or_low_in_window_is_skipped_not_crashed():
    candles = _tight_window()
    candles[2] = _candle(0.0, 0.0)
    # Should not raise, and the corrupt window itself can't score as congestion.
    assert ld.get_congestion_zones(candles) == []


def test_scan_resumes_past_a_detected_zone_not_overlapping():
    """Two consecutive tight windows back-to-back should yield two separate
    zones, not one-per-candle overlapping duplicates -- confirms the scan
    advances by CONGESTION_WINDOW after a hit rather than by 1."""
    candles = _tight_window() + [
        _candle(4102.0, 4100.0),
        _candle(4103.0, 4101.0),
        _candle(4101.5, 4100.5),
        _candle(4102.5, 4100.8),
        _candle(4102.0, 4100.2),
    ]
    zones = ld.get_congestion_zones(candles)
    assert len(zones) == 2
    assert zones[0]["price"] == 4001.5
    assert zones[1]["price"] == 4101.5


def test_get_candidate_levels_includes_nearby_congestion_zone(monkeypatch):
    """Wiring check: get_candidate_levels() merges congestion zones in when
    within 50pts of current price, using the same distance window as round
    numbers (see the comment above the congestion loop). Other sources are
    stubbed out so a real asia/round level at the same price can't absorb
    the congestion entry in dedup and mask the assertion."""
    monkeypatch.setattr(ld, "get_asia_range", lambda h1_candles: (0.0, 0.0))
    monkeypatch.setattr(ld, "get_swing_levels", lambda h1_candles: [])
    monkeypatch.setattr(ld, "get_round_levels", lambda price, count=6: [])
    # current_price offset from the zone's own price (4001.5) -- dist < 1.0
    # is treated as "already inside the level" and skipped, so exact price
    # would never appear in the output.
    candidates = ld.get_candidate_levels(_tight_window(), current_price=4006.5)
    assert any(c["type"] == "congestion" and c["price"] == 4001.5 for c in candidates)


def test_get_candidate_levels_excludes_far_congestion_zone():
    candidates = ld.get_candidate_levels(_tight_window(), current_price=4200.0)
    assert not any(c["type"] == "congestion" for c in candidates)
