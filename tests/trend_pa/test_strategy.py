"""Trend PA's setup, from synthetic closed candles. Nothing here reaches a
broker or a database.

The happy path is one hand-built BUY: an H4 staircase up, an H1 swing low at
2001.0, and an M15 pullback from 2012 that prints a bullish engulfing on that
level. Every refusal test changes ONE thing about it, so a refusal cannot be
passing for a reason other than the one it names.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from backend.src.services.trend_pa import strategy as st

TUESDAY_10 = datetime(2026, 9, 29, 10, 0, tzinfo=timezone.utc)


def _bar(o, c, up=0.2, down=0.2, ts=0):
    return {"ts": ts, "open": o, "close": c,
            "high": max(o, c) + up, "low": min(o, c) - down}


def _path(prices, w=0.2):
    out, prev = [], prices[0]
    for i, p in enumerate(prices[1:]):
        out.append(_bar(prev, p, w, w, ts=i))
        prev = p
    return out


def h4_up():
    prices, p = [1900.0], 1900.0
    for _ in range(13):
        for _ in range(3):
            p += 10
            prices.append(p)
        for _ in range(2):
            p -= 8
            prices.append(p)
    prices += [p + 10, p + 20]
    return _path(prices)


def h1_support_at_2001():
    down = [2030 - i * (28.8 / 20) for i in range(21)]  # to 2001.2
    up = [2001.2 + (i + 1) * 0.7 for i in range(20)]
    return _path(down + up)                               # low 2001.0


def m15_engulfing():
    bars, p = [], 2004.0
    for _ in range(16):
        bars.append({"ts": 0, "open": p, "close": p + 0.5,
                     "high": p + 0.5 + 0.75, "low": p - 0.75})
        p += 0.5
    for _ in range(11):
        bars.append({"ts": 0, "open": p, "close": p - 1,
                     "high": p + 0.5, "low": p - 1.5})
        p -= 1
    bars.append({"ts": 0, "open": 2001.9, "close": 2000.8, "high": 2002.1, "low": 2000.4})
    bars.append({"ts": 0, "open": 2000.7, "close": 2002.4, "high": 2002.6, "low": 2000.2})
    return bars


def happy():
    return h4_up(), h1_support_at_2001(), m15_engulfing()


# ── primitives ───────────────────────────────────────────────────────────────

def test_swings_need_bars_on_both_sides():
    # Bar i runs prices[i] -> prices[i+1]. The 3->4 bar and the 4->3 bar share
    # a high; the first of them is the swing.
    bars = _path([1, 2, 3, 4, 3, 2, 1, 2, 3, 4])
    highs, lows = st.swings(bars, 2)
    assert [i for i, _ in highs] == [2]
    assert [i for i, _ in lows] == [5]


def test_the_last_n_bars_never_hold_a_swing():
    bars = _path([1, 2, 3, 4, 5, 4])
    assert st.swings(bars, 2) == ([], [])


def test_ema_refuses_to_guess_without_history():
    assert st.ema([1.0] * 49, 50) is None
    assert st.ema([2.0] * 50, 50) == pytest.approx(2.0)


def test_the_staircase_is_an_uptrend():
    assert st.trend(h4_up(), st.DEFAULTS)[0] == "up"


def test_the_mirrored_staircase_is_a_downtrend():
    mirrored = [{k: (4000 - v if k in ("open", "close") else v) for k, v in b.items()}
                for b in h4_up()]
    for b, src in zip(mirrored, h4_up()):
        b["high"], b["low"] = 4000 - src["low"], 4000 - src["high"]
    assert st.trend(mirrored, st.DEFAULTS)[0] == "down"


def test_a_lower_high_breaks_the_uptrend():
    """The staircase up to its second-to-last trough, then a rally that fails
    below the previous swing high: higher low, LOWER high -- not a trend."""
    bars = h4_up()
    highs, lows = st.swings(bars, 2)
    _, last_h = highs[-2]
    trough_i, _ = lows[-2]
    kept = bars[:trough_i + 1]
    p = kept[-1]["close"]
    rally = [p + 6, last_h - 2.4, last_h - 6, last_h - 10, last_h - 7, last_h - 5]
    bars = kept + _path([p] + rally)
    new_highs, _ = st.swings(bars, 2)
    assert new_highs[-1][1] < new_highs[-2][1]
    assert st.trend(bars, st.DEFAULTS)[0] == "none"


def test_the_ema_filter_can_veto_a_structural_trend(monkeypatch):
    bars = h4_up()
    monkeypatch.setattr(st, "ema", lambda values, period: 10_000.0)
    assert st.trend(bars, dict(st.DEFAULTS, require_ema=False))[0] == "up"
    assert st.trend(bars, st.DEFAULTS)[0] == "none"


@pytest.mark.parametrize("when,expected", [
    (datetime(2026, 9, 29, 7, 59, tzinfo=timezone.utc), None),      # Asia
    (datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc), "london"),
    (datetime(2026, 9, 29, 13, 0, tzinfo=timezone.utc), "overlap"),
    (datetime(2026, 9, 29, 16, 0, tzinfo=timezone.utc), "new_york"),
    (datetime(2026, 9, 29, 21, 0, tzinfo=timezone.utc), None),
    (datetime(2026, 10, 2, 18, 59, tzinfo=timezone.utc), "new_york"),  # Friday
    (datetime(2026, 10, 2, 19, 0, tzinfo=timezone.utc), None),          # Friday late
    (datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc), None),          # Saturday
])
def test_sessions(when, expected):
    assert st.session_of(when, st.DEFAULTS) == expected


def test_a_broken_swing_high_is_a_floor_in_an_uptrend():
    bars = _path([10, 11, 12, 13, 12, 11, 12, 13, 14, 15, 16])
    kinds = {k for _, k in st.levels(bars, "BUY", 15.5, st.DEFAULTS)}
    assert "swing_high" in kinds


@pytest.mark.parametrize("prev,cur,direction,expected", [
    (_bar(10, 9), _bar(8.9, 10.2), "BUY", "engulfing"),
    (_bar(9, 10), _bar(10.1, 8.8), "SELL", "engulfing"),
    (_bar(10, 9), {"open": 9.8, "close": 10.0, "high": 10.05, "low": 8.0}, "BUY", "pin"),
    (_bar(9, 10), {"open": 10.2, "close": 10.0, "high": 12.0, "low": 9.95}, "SELL", "pin"),
    (_bar(10, 9), _bar(9, 9.5), "BUY", None),           # inside bar
    (_bar(10, 9), _bar(8.9, 10.2), "SELL", None),       # wrong direction
])
def test_patterns(prev, cur, direction, expected):
    assert st.pattern(prev, cur, direction, st.DEFAULTS) == expected


# ── the decision ─────────────────────────────────────────────────────────────

def test_the_hand_built_buy_is_taken():
    s = st.evaluate(*happy(), TUESDAY_10)
    assert isinstance(s, st.Setup), s
    assert s.direction == "BUY" and s.pattern == "engulfing"
    assert s.level == pytest.approx(2001.0)
    assert s.entry == pytest.approx(2002.4)


def test_the_stop_is_beyond_the_pullback_low_and_the_target_is_2r():
    s = st.evaluate(*happy(), TUESDAY_10)
    lowest = min(b["low"] for b in m15_engulfing()[-6:])
    assert s.stop_loss < lowest
    assert s.stop_loss == pytest.approx(lowest - 0.1 * s.atr_m15)
    assert s.take_profit - s.entry == pytest.approx(2.0 * (s.entry - s.stop_loss))


def test_the_features_are_all_numbers():
    s = st.evaluate(*happy(), TUESDAY_10)
    assert len(s.features) == 14
    assert all(isinstance(v, float) for v in s.features.values())


def test_outside_the_sessions_nothing_is_taken():
    assert st.evaluate(*happy(), datetime(2026, 9, 29, 3, 0, tzinfo=timezone.utc)) \
        == "outside London/New York"


def test_no_trend_no_trade():
    h4, h1, m15 = happy()
    flat = [_bar(2000, 2000.1) for _ in range(len(h4))]
    assert "trend" in st.evaluate(flat, h1, m15, TUESDAY_10)


def test_a_big_breakout_candle_is_not_chased():
    h4, h1, m15 = happy()
    m15[-1]["high"] = m15[-1]["low"] + 10
    assert "chasing" in st.evaluate(h4, h1, m15, TUESDAY_10)


def test_without_a_pullback_there_is_no_entry():
    h4, h1, m15 = happy()
    p = dict(st.DEFAULTS, min_pullback_atr=50)
    assert "no pullback" in st.evaluate(h4, h1, m15, TUESDAY_10, p)


def test_a_candle_away_from_any_level_is_refused():
    h4, _h1, m15 = happy()
    far = _path([2030 - i for i in range(41)])  # no swing at all
    assert "not at an H1 support" in st.evaluate(h4, far, m15, TUESDAY_10)


def test_a_close_too_far_past_the_level_is_chasing():
    h4, h1, m15 = happy()
    assert "chasing" in st.evaluate(h4, h1, m15, TUESDAY_10, dict(st.DEFAULTS, max_chase_atr=0.1))


def test_a_stop_too_wide_is_refused():
    assert "too wide" in st.evaluate(*happy(), TUESDAY_10, dict(st.DEFAULTS, max_sl_atr=0.6))


def test_a_stop_too_tight_is_refused():
    assert "too tight" in st.evaluate(*happy(), TUESDAY_10, dict(st.DEFAULTS, min_sl_atr=5))


def test_evaluate_does_not_mutate_its_input():
    h4, h1, m15 = happy()
    before = copy.deepcopy((h4, h1, m15))
    st.evaluate(h4, h1, m15, TUESDAY_10)
    assert (h4, h1, m15) == before


def test_short_history_is_refused_not_guessed():
    h4, h1, m15 = happy()
    assert st.evaluate(h4[:10], h1, m15, TUESDAY_10) == "not enough history"


def test_an_equal_pair_of_highs_is_one_swing_not_two():
    """Found building this file: the staircase's peak bar and the next bar
    shared a high, both counted, the last two swing highs compared equal, and
    a clean uptrend read as none."""
    bars = [_bar(1, 2), _bar(2, 3), _bar(3, 5, up=0), _bar(5, 4, up=0),
            _bar(4, 3), _bar(3, 2), _bar(2, 1)]
    highs, _ = st.swings(bars, 2)
    assert [i for i, _ in highs] == [2]


def test_a_bullish_candle_that_does_not_clear_the_prior_open_is_not_engulfing():
    prev = _bar(10, 9.6)
    cur = _bar(9.5, 9.95)
    assert st.pattern(prev, cur, "BUY", st.DEFAULTS) is None


def test_higher_highs_with_a_lower_low_is_not_an_uptrend():
    bars = h4_up()
    _, lows = st.swings(bars, 2)
    i, _ = lows[-1]
    _, prev_l = lows[-2]
    bars[i]["low"] = prev_l - 5
    assert st.swings(bars, 2)[1][-1][1] < prev_l
    assert st.trend(bars, st.DEFAULTS)[0] == "none"


def test_a_candle_that_pierces_too_far_through_the_level_is_a_break_not_a_test():
    h4, h1, m15 = happy()
    m15[-1]["low"] = 2001.0 - 3.0
    # The candle is now big too; lift that limit so the pierce is what refuses.
    wide = dict(st.DEFAULTS, max_candle_atr=10)
    assert "not at an H1 support" in st.evaluate(h4, h1, m15, TUESDAY_10, wide)


def test_a_level_the_candle_never_came_near_does_not_count():
    h4, _h1, m15 = happy()
    down = [2030 - i * (34.8 / 20) for i in range(21)]   # to 1995.2
    up = [1995.2 + (i + 1) * 1.0 for i in range(20)]
    far_below = _path(down + up)                          # swing low 1995.0
    assert "not at an H1 support" in st.evaluate(h4, far_below, m15, TUESDAY_10)


def test_of_two_levels_touched_the_nearest_is_the_one_traded(monkeypatch):
    monkeypatch.setattr(st, "levels", lambda *a, **k: [(2000.5, "swing_high"),
                                                        (2001.0, "swing_low")])
    s = st.evaluate(*happy(), TUESDAY_10)
    assert s.level == pytest.approx(2001.0) and s.level_kind == "swing_low"


# ── why there is no trend ────────────────────────────────────────────────────

def test_a_lower_high_is_named_in_the_refusal():
    """A refusal that only says "no clear H4 trend" cannot be checked against
    the chart; the reason carries what the strategy saw. The bare phrase stays
    the prefix, because backtest.reason_key groups refusals on it."""
    bars = h4_up()
    highs, lows = st.swings(bars, 2)
    _, last_h = highs[-2]
    trough_i, _ = lows[-2]
    kept = bars[:trough_i + 1]
    p = kept[-1]["close"]
    bars = kept + _path([p] + [p + 6, last_h - 2.4, last_h - 6, last_h - 10, last_h - 7, last_h - 5])
    t, facts = st.trend(bars, st.DEFAULTS)
    assert t == "none"
    assert "highs falling" in facts["why"] and "lows rising" in facts["why"]


def test_an_ema_veto_is_named_in_the_refusal(monkeypatch):
    monkeypatch.setattr(st, "ema", lambda values, period: 10_000.0)
    t, facts = st.trend(h4_up(), st.DEFAULTS)
    assert t == "none" and "EMA50" in facts["why"]


def test_too_few_swings_is_named_in_the_refusal():
    flat = [_bar(2000, 2000.1) for _ in range(120)]
    t, facts = st.trend(flat, st.DEFAULTS)
    assert t == "none" and "swing" in facts["why"]


def test_evaluate_puts_the_why_in_brackets_after_the_grouping_phrase():
    h4, h1, m15 = happy()
    flat = [_bar(2000, 2000.1) for _ in range(len(h4))]
    reason = st.evaluate(flat, h1, m15, TUESDAY_10)
    assert reason.startswith("no clear H4 trend (") and reason.endswith(")")
