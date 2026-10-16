"""The session-drift trials (docs/todo/018).

Pure and offline. Pinned: the candle decoder reads O,C,L,H in that order,
each trial trades exactly its window and charges the cost once, the 200-day
rule never reads today's close, days with missing minutes are dropped rather
than filled, and the verdict fails a series that misses any one condition.
"""
import lzma
import struct
from datetime import datetime, timezone

import pytest

from backend.src.services.backtest import drift_trials as dt
from backend.src.services.reversal_engine.entry_study import Bar

COST = 0.5


def _day(y, mo, d):
    return datetime(y, mo, d, tzinfo=timezone.utc).timestamp()


def _blob(records):
    raw = b"".join(struct.pack(">IIIIIf", *r) for r in records)
    return lzma.compress(raw)


def _flat_day(day_ts, price=100.0, volume=1.0, moves=None):
    """1,440 one-minute bars at `price`; `moves` maps minute -> (open, close)."""
    out = []
    for m in range(1440):
        o, c = (moves or {}).get(m, (price, price))
        out.append(Bar(day_ts + m * 60, o, max(o, c), min(o, c), c, volume))
    return out


class TestDecodeCandles:
    def test_fields_are_open_close_low_high_in_that_order(self):
        day = _day(2019, 1, 2)
        bars = dt.decode_candles(_blob([(60, 1000500, 1002000, 999000, 1003000, 2.5)]), day)
        assert bars == [Bar(day + 60, 1000.5, 1003.0, 999.0, 1002.0, 2.5)]

    def test_an_empty_body_is_no_bars(self):
        assert dt.decode_candles(b"", _day(2019, 1, 2)) == []

    def test_url_month_is_zero_based(self):
        assert dt.candle_url("XAUUSD", _day(2019, 1, 2)).endswith(
            "/XAUUSD/2019/00/02/BID_candles_min_1.bi5")


class TestWindowTrade:
    def test_asian_long_enters_at_00_open_exits_at_0659_close_and_pays_cost_once(self):
        day = _day(2024, 3, 5)          # a Tuesday
        bars = _flat_day(day, moves={0: (100.0, 100.0), 418: (103.0, 103.0),
                                     419: (103.0, 104.0), 420: (110.0, 110.0)})
        (t,), dropped = dt.asian_long({day: bars}, COST)
        assert dropped == 0
        assert t["ts"] == day and t["side"] == 1
        assert t["r"] == pytest.approx(104.0 - 100.0 - COST)

    def test_one_hour_without_a_tick_drops_an_asian_day(self):
        day = _day(2024, 3, 5)
        bars = _silence(_flat_day(day), [3])
        trades, dropped = dt.asian_long({day: bars}, COST)
        assert trades == [] and dropped == 1

    def test_a_thin_hour_with_one_traded_minute_is_not_missing(self):
        day = _day(2024, 3, 5)
        bars = _silence(_flat_day(day), list(range(7)), keep_minute=17)
        trades, dropped = dt.asian_long({day: bars}, COST)
        assert len(trades) == 1 and dropped == 0

    def test_friday_keeps_two_missing_hours_and_drops_three(self):
        fri = _day(2024, 3, 8)
        two = dt.friday_long({fri: _silence(_flat_day(fri), [5, 9])}, COST)
        three = dt.friday_long({fri: _silence(_flat_day(fri), [5, 9, 11])}, COST)
        assert (len(two[0]), two[1]) == (1, 0)
        assert (len(three[0]), three[1]) == (0, 1)

    def test_an_absent_hour_counts_as_missing(self):
        day = _day(2024, 3, 5)
        bars = [b for b in _flat_day(day) if not (180 <= (b.ts - day) // 60 < 240)]
        trades, dropped = dt.asian_long({day: bars}, COST)
        assert trades == [] and dropped == 1

    def test_an_absent_entry_minute_drops_the_day(self):
        day = _day(2024, 3, 5)
        trades, dropped = dt.asian_long({day: _flat_day(day)[1:]}, COST)
        assert trades == [] and dropped == 1

    def test_asian_long_skips_weekends(self):
        sat, sun = _day(2024, 3, 9), _day(2024, 3, 10)
        trades, _ = dt.asian_long({sat: _flat_day(sat), sun: _flat_day(sun)}, COST)
        assert trades == []

    def test_friday_long_trades_only_fridays_0000_to_1959(self):
        thu, fri = _day(2024, 3, 7), _day(2024, 3, 8)
        fri_bars = _flat_day(fri, moves={0: (50.0, 50.0), 1199: (50.0, 57.0),
                                         1200: (80.0, 80.0)})
        trades, _ = dt.friday_long({thu: _flat_day(thu), fri: fri_bars}, COST)
        assert [t["ts"] for t in trades] == [fri]
        assert trades[0]["r"] == pytest.approx(57.0 - 50.0 - COST)


def _silence(bars, hours, keep_minute=None):
    """Volume 0 in the given hours of the day, except minute `keep_minute`
    of each of them."""
    out = []
    for b in bars:
        m = int(b.ts % 86400) // 60
        quiet = m // 60 in hours and m % 60 != keep_minute
        out.append(Bar(b.ts, b.open, b.high, b.low, b.close, 0.0) if quiet else b)
    return out


def _daily(closes, start=(2020, 1, 6)):
    """Weekday daily rows (ts, open, close) with open == previous close."""
    out, ts, prev = [], _day(*start), closes[0]
    for c in closes:
        while datetime.fromtimestamp(ts, timezone.utc).weekday() >= 5:
            ts += 86400
        out.append((ts, prev, c))
        prev = c
        ts += 86400
    return out


class TestDailyBars:
    def test_open_is_the_00_record_and_close_the_last_traded_minute(self):
        day = _day(2024, 3, 5)
        bars = _flat_day(day, moves={0: (10.0, 11.0), 1300: (20.0, 21.0)})
        bars = bars[:1301] + [Bar(b.ts, 99.0, 99.0, 99.0, 99.0, 0.0) for b in bars[1301:]]
        (row,), dropped = dt.daily_bars({day: bars})
        assert row == (day, 10.0, 21.0) and dropped == 0

    def test_a_weekday_with_11_traded_hours_is_dropped_and_sunday_is_not_a_day(self):
        tue, sun = _day(2024, 3, 5), _day(2024, 3, 10)
        thin = _silence(_flat_day(tue), list(range(11, 24)))
        rows, dropped = dt.daily_bars({tue: thin, sun: _flat_day(sun)})
        assert rows == [] and dropped == 1

    def test_a_weekday_with_12_traded_hours_each_one_minute_thin_counts(self):
        tue = _day(2024, 3, 5)
        thin = _silence(_silence(_flat_day(tue), list(range(12, 24))),
                        list(range(12)), keep_minute=0)
        rows, dropped = dt.daily_bars({tue: thin})
        assert len(rows) == 1 and dropped == 0


class TestSma200:
    def test_needs_200_prior_days(self):
        assert dt.sma200_trades(_daily([100.0] * 200), COST) == []
        assert len(dt.sma200_trades(_daily([100.0] * 201), COST)) == 1

    def test_side_comes_from_yesterday_not_today(self):
        # 200 days at 100, yesterday 101 (above its SMA), today collapses to 50.
        rows = _daily([100.0] * 200 + [101.0, 50.0])
        last = dt.sma200_trades(rows, COST)[-1]
        assert last["ts"] == rows[-1][0]
        assert last["side"] == 1                       # a look-ahead would go short
        assert last["r"] == pytest.approx((50.0 - 101.0) - COST)

    def test_short_side_profits_from_a_fall(self):
        rows = _daily([100.0] * 200 + [99.0, 90.0])
        last = dt.sma200_trades(rows, COST)[-1]
        assert last["side"] == -1
        assert last["r"] == pytest.approx((99.0 - 90.0) - COST)

    def test_asian_above_keeps_only_days_the_200_day_says_long(self):
        asian = [{"ts": 1.0, "r": 1.0, "side": 1}, {"ts": 2.0, "r": 2.0, "side": 1},
                 {"ts": 3.0, "r": 3.0, "side": 1}]
        d3 = [{"ts": 1.0, "side": 1}, {"ts": 2.0, "side": -1}]
        assert [t["ts"] for t in dt.asian_above_sma(asian, d3)] == [1.0]


class TestBearWindow:
    def test_each_bear_year_must_close_below_the_year_before(self):
        rows = [(_day(y, 12, 31), 0.0, c) for y, c in
                [(2012, 1600.0), (2013, 1200.0), (2014, 1180.0), (2015, 1060.0)]]
        ok, closes = dt.bear_window_ok(rows)
        assert ok and closes[2015] == 1060.0

    def test_one_up_year_fails_it(self):
        rows = [(_day(y, 12, 31), 0.0, c) for y, c in
                [(2012, 1600.0), (2013, 1200.0), (2014, 1250.0), (2015, 1060.0)]]
        assert dt.bear_window_ok(rows)[0] is False


def _year_trades(per_year_mean, n=200, sd=1.0):
    """Deterministic trades: per year, alternating mean +/- sd."""
    out = []
    for y, m in per_year_mean.items():
        t0 = _day(y, 1, 2)
        for i in range(n):
            out.append({"ts": t0 + i * 86400, "r": m + (sd if i % 2 else -sd), "side": 1})
    return out


GOOD = {y: 0.3 for y in range(2003, 2026)}


class TestVerdict:
    def test_a_steady_edge_in_every_year_is_confirmed(self):
        v = dt.verdict(_year_trades(GOOD))
        assert v["confirmed"], v["why"]

    @pytest.mark.parametrize("name,means", [
        ("bear window negative", {**GOOD, 2013: -1.0, 2014: -1.0, 2015: -1.0}),
        ("pre-2019 negative", {**GOOD, 2003: -6.0, 2004: -6.0, 2005: -6.0,
                               **{y: 3.0 for y in range(2019, 2026)}}),
        ("2019 onward negative", {**{y: 1.0 for y in range(2003, 2026)},
                                  2019: -6.0, **{y: 0.3 for y in range(2020, 2026)}}),
        ("too few positive years", {**{y: (2.0 if y % 3 == 0 else -0.2) for y in range(2003, 2026)},
                                    2013: 2.0, 2014: 2.0, 2015: 2.0}),
    ])
    def test_each_condition_alone_fails_it(self, name, means):
        v = dt.verdict(_year_trades(means))
        assert not v["confirmed"], name

    def test_a_t_below_the_bar_fails_it(self):
        v = dt.verdict(_year_trades({y: 0.02 for y in range(2003, 2026)}, sd=1.0))
        assert v["t"] < dt.T_BAR and not v["confirmed"]

    def test_years_with_under_30_trades_do_not_vote(self):
        trades = _year_trades(GOOD) + _year_trades({2026: -5.0}, n=10)
        v = dt.verdict(trades)
        assert 2026 not in v["voting_years"]

    def test_the_bar_is_bonferroni_for_four_trials(self):
        assert dt.T_BAR == pytest.approx(2.2414, abs=1e-3)


class TestRunnerOrder:
    """tools/drift_trials.run_trials: the bear window is judged before any
    trial is computed, and a failed one stops the run."""

    @staticmethod
    def _days(year_close):
        out = []
        for y, close in year_close.items():
            ts = _day(y, 1, 1)
            while ts < _day(y + 1, 1, 1):
                if datetime.fromtimestamp(ts, timezone.utc).weekday() < 5:
                    out.append((ts, [Bar(ts + h * 3600, close, close, close, close, 1.0)
                                     for h in range(24)]))
                ts += 86400
        return out

    def test_a_bear_window_that_does_not_hold_stops_before_any_trial(self, monkeypatch):
        from tools import drift_trials as tool
        called = []
        monkeypatch.setattr(tool.dt, "sma200_trades", lambda *a, **k: called.append(1) or [])
        monkeypatch.setattr(tool.dt, "verdict", lambda *a, **k: called.append(1) or {})
        lines = []
        out = tool.run_trials(self._days({2012: 1600.0, 2013: 1700.0, 2014: 1500.0,
                                          2015: 1400.0}), COST, lines.append)
        assert out["bear_ok"] is False and called == []
        assert not any(line.startswith("D1") for line in lines)

    def test_a_bear_window_that_holds_runs_all_four(self):
        from tools import drift_trials as tool
        lines = []
        out = tool.run_trials(self._days({2012: 1600.0, 2013: 1500.0, 2014: 1400.0,
                                          2015: 1300.0}), COST, lines.append)
        assert out["bear_ok"] is True and set(out["trials"]) == {"D1", "D2", "D3", "D4"}
