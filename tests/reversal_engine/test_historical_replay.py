"""Dukascopy replay: bars from ticks, the resumable download, and signals
regenerated from bars alone (docs/todo/reversal-engine/260).

Pure and offline. The download takes an injected fetch function, so nothing
here touches the network. The properties pinned are the ones that would make
the research lie: a wrong price scale or tick order, a bar built from ticks
of the next minute, a download that records a failure as "no data" and never
retries it, and regenerated signals that read a bar from after the cycle.
"""
import lzma
from datetime import datetime, timezone
import random
import struct

import pytest

from backend.src.services.reversal_engine import entry_study as es
from backend.src.services.reversal_engine import historical_bars as hb
from backend.src.services.reversal_engine import historical_signals as hs

H0 = 1_704_186_000.0  # 2024-01-02 09:00:00 UTC, an hour boundary


def _blob(ticks):
    """(ms, ask_int, bid_int) -> a Dukascopy hour file: LZMA of big-endian
    uint32 ms, uint32 ask, uint32 bid, float32 ask volume, float32 bid volume."""
    raw = b"".join(struct.pack(">IIIff", ms, a, b, 1.0, 1.0) for ms, a, b in ticks)
    return lzma.compress(raw)


class TestDecode:
    def test_a_tick_is_read_with_ask_before_bid_and_prices_over_one_thousand(self):
        t = hb.decode_bi5(_blob([(500, 2077255, 2076965)]), H0)
        assert len(t) == 1
        assert t[0].ts == H0 + 0.5
        assert t[0].ask == pytest.approx(2077.255)
        assert t[0].bid == pytest.approx(2076.965)

    def test_an_empty_file_is_no_ticks(self):
        assert hb.decode_bi5(b"", H0) == []

    def test_a_negative_control_a_swapped_reading_would_put_bid_above_ask(self):
        t = hb.decode_bi5(_blob([(0, 2077255, 2076965)]), H0)[0]
        assert t.ask > t.bid


class TestMinuteBars:
    def test_ohlc_is_the_bid_and_volume_is_the_tick_count(self):
        ticks = hb.decode_bi5(_blob([(1000, 2000100, 2000000), (20000, 2000600, 2000500),
                                     (40000, 2000000, 1999900), (59999, 2000300, 2000200)]), H0)
        bars = hb.ticks_to_m1(ticks)
        assert len(bars) == 1
        b = bars[0]
        assert (b.ts, b.open, b.high, b.low, b.close, b.volume) == (
            H0, 2000.0, 2000.5, 1999.9, 2000.2, 4.0)

    def test_a_tick_at_the_next_second_boundary_belongs_to_the_next_minute(self):
        ticks = hb.decode_bi5(_blob([(59999, 2000100, 2000000), (60000, 2001100, 2001000)]), H0)
        bars = hb.ticks_to_m1(ticks)
        assert [b.ts for b in bars] == [H0, H0 + 60]
        assert bars[0].close == 2000.0 and bars[1].open == 2001.0

    def test_a_minute_without_ticks_has_no_bar(self):
        ticks = hb.decode_bi5(_blob([(0, 2000100, 2000000), (180000, 2000100, 2000000)]), H0)
        assert [b.ts for b in hb.ticks_to_m1(ticks)] == [H0, H0 + 180]

    def test_ticks_out_of_order_still_give_the_first_as_open(self):
        from backend.src.services.reversal_engine.historical_bars import Tick
        bars = hb.ticks_to_m1([Tick(H0 + 30, 2000.0, 2001.0), Tick(H0 + 5, 1990.0, 1991.0)])
        assert bars[0].open == 1990.0 and bars[0].close == 2000.0


class TestAggregate:
    def _m1(self, n, start=H0):
        return [es.Bar(start + 60 * i, 2000 + i, 2000 + i + 2, 2000 + i - 1, 2000 + i + 1, 1.0)
                for i in range(n)]

    def test_buckets_align_to_the_epoch_not_to_the_first_bar(self):
        bars = hb.aggregate(self._m1(90, start=H0 + 600), 3600)
        assert [b.ts % 3600 for b in bars] == [0, 0]
        assert bars[0].ts == H0

    def test_a_bucket_takes_first_open_last_close_and_the_extremes(self):
        bars = hb.aggregate(self._m1(60), 3600)
        assert len(bars) == 1
        b = bars[0]
        assert (b.open, b.close, b.high, b.low) == (2000, 2060, 2061, 1999)
        assert b.volume == 60.0


class TestDownload:
    def test_hours_are_fetched_once_and_a_second_run_fetches_nothing(self, tmp_path):
        calls = []

        def fetch(url):
            calls.append(url)
            return _blob([(0, 2000100, 2000000)])
        r1 = hb.download_range(fetch, tmp_path, "XAUUSD", H0, H0 + 3 * 3600)
        assert r1.fetched == 3 and len(calls) == 3
        r2 = hb.download_range(fetch, tmp_path, "XAUUSD", H0, H0 + 3 * 3600)
        assert r2.fetched == 0 and r2.cached == 3 and len(calls) == 3

    def test_the_url_month_is_zero_based(self):
        assert hb.hour_url("XAUUSD", H0).endswith("/XAUUSD/2024/00/02/09h_ticks.bi5")

    def test_no_data_is_remembered_so_it_is_not_asked_for_again(self, tmp_path):
        calls = []

        def fetch(url):
            calls.append(url)
            return None
        r = hb.download_range(fetch, tmp_path, "XAUUSD", H0, H0 + 3600)
        assert r.empty == 1 and r.failed == []
        hb.download_range(fetch, tmp_path, "XAUUSD", H0, H0 + 3600)
        assert len(calls) == 1

    def test_a_failure_is_reported_and_retried_not_recorded_as_no_data(self, tmp_path):
        state = {"n": 0}

        def fetch(url):
            state["n"] += 1
            if state["n"] <= 1:
                raise OSError("connection reset")
            return _blob([(0, 2000100, 2000000)])
        r1 = hb.download_range(fetch, tmp_path, "XAUUSD", H0, H0 + 3600, retries=0)
        assert len(r1.failed) == 1 and r1.fetched == 0
        r2 = hb.download_range(fetch, tmp_path, "XAUUSD", H0, H0 + 3600, retries=0)
        assert r2.fetched == 1 and r2.failed == []

    def test_load_m1_reads_what_was_downloaded_in_time_order(self, tmp_path):
        def fetch(url):
            return _blob([(0, 2000100, 2000000), (61000, 2000200, 2000100)])
        hb.download_range(fetch, tmp_path, "XAUUSD", H0, H0 + 2 * 3600)
        bars = hb.load_m1(tmp_path, "XAUUSD", H0, H0 + 2 * 3600)
        assert [b.ts for b in bars] == [H0, H0 + 60, H0 + 3600, H0 + 3660]


def _market(days, seed=7, sigma=0.35):
    """A deterministic random walk with no edge in it, M1 bars."""
    rng = random.Random(seed)
    px, out = 2000.0, []
    t0 = 1_704_067_200.0  # 2024-01-01 00:00 UTC
    for i in range(days * 1440):
        o = px
        path = [o]
        for _ in range(3):
            path.append(path[-1] + rng.gauss(0, sigma))
        px = path[-1]
        out.append(es.Bar(t0 + 60 * i, o, max(path) + 0.1, min(path) - 0.1, px, 10.0))
    return out


class TestRegeneratedSignals:
    def test_it_finds_signals_in_a_market_that_moves_and_none_in_a_dead_one(self):
        live = hs.regenerate(_market(8))
        assert len(live) > 5
        dead = [es.Bar(1_704_067_200.0 + 60 * i, 2000, 2000.05, 1999.95, 2000, 1.0)
                for i in range(8 * 1440)]
        assert hs.regenerate(dead) == []

    def test_nothing_after_a_cutoff_changes_what_was_signalled_before_it(self):
        full = _market(8)
        cut = full[: 6 * 1440]
        a = hs.regenerate(full)
        b = hs.regenerate(cut)
        horizon = cut[-1].ts - 3600
        key = lambda g: (g.sig.created_at, g.sig.direction, g.sig.level_price, g.score)
        assert [key(g) for g in a if g.sig.created_at < horizon] == \
               [key(g) for g in b if g.sig.created_at < horizon]
        assert any(g.sig.created_at < horizon for g in a)

    def test_a_future_shock_in_the_bars_cannot_move_an_earlier_signal(self):
        base = _market(8)
        shocked = list(base)
        for i in range(6 * 1440, len(shocked)):
            b = shocked[i]
            shocked[i] = es.Bar(b.ts, b.open + 60, b.high + 60, b.low + 60, b.close + 60, b.volume)
        horizon = base[6 * 1440].ts
        pre = lambda gs: [(g.sig.created_at, g.sig.level_price) for g in gs
                          if g.sig.created_at < horizon]
        assert pre(hs.regenerate(base)) == pre(hs.regenerate(shocked))

    def test_every_signal_is_scored_at_least_half_and_the_zone_hugs_its_level(self):
        gens = hs.regenerate(_market(8))
        assert gens
        for g in gens:
            assert g.score >= 0.5
            assert 2.0 <= g.atr <= 80.0
            s = g.sig
            if s.level_type == "unicorn":
                # GD2 carries its own confluence zone (build_signal), not the
                # level +/- pad; the level sits inside it.
                assert s.entry_low <= s.level_price <= s.entry_high
            elif s.direction == "BUY":
                assert s.entry_low == pytest.approx(s.level_price - 1.0, abs=0.011)
            else:
                assert s.entry_high == pytest.approx(s.level_price + 1.0, abs=0.011)
        assert any(g.sig.level_type != "unicorn" for g in gens)

    def test_a_level_does_not_re_signal_inside_the_cooldown(self):
        gens = hs.regenerate(_market(10), cooldown_s=1800.0)
        for i, a in enumerate(gens):
            for b in gens[i + 1:]:
                if (b.sig.direction == a.sig.direction
                        and abs(b.sig.level_price - a.sig.level_price) <= 3.0):
                    # The live check is `created_at > now - cooldown`, so a
                    # signal exactly one cooldown old no longer blocks.
                    assert b.sig.created_at - a.sig.created_at >= 1800.0

    def test_a_longer_cooldown_cannot_make_more_signals(self):
        short = len(hs.regenerate(_market(10), cooldown_s=600.0))
        long_ = len(hs.regenerate(_market(10), cooldown_s=7200.0))
        assert long_ <= short

    def test_ids_are_unique_and_signals_are_in_time_order(self):
        gens = hs.regenerate(_market(8))
        ids = [g.sig.id for g in gens]
        assert len(set(ids)) == len(ids)
        ts = [g.sig.created_at for g in gens]
        assert ts == sorted(ts)


class TestTheAtrMirror:
    def test_the_local_atr_equals_the_engines_own_on_the_same_candles(self):
        from backend.src.services.reversal_engine.reversal_engine_service import ReversalEngine
        bars = _market(2)[:600]
        candles = [hs._candle(b) for b in hb.aggregate(bars, 900)]
        assert hs._calc_atr(candles) == ReversalEngine._calc_atr(candles)
        assert hs._calc_atr(candles) > 0


class TestWeekendHoursAreNotAskedFor:
    def test_saturday_is_skipped_and_a_weekday_is_not(self, tmp_path):
        sat = datetime(2024, 1, 6, 15, tzinfo=timezone.utc).timestamp()  # a Saturday
        calls = []
        r = hb.download_range(lambda u: calls.append(u), tmp_path, "XAUUSD", sat, sat + 3600)
        assert calls == [] and r.skipped_closed == 1
        r = hb.download_range(lambda u: calls.append(u), tmp_path, "XAUUSD", H0, H0 + 3600)
        assert len(calls) == 1 and r.skipped_closed == 0


class TestBeingPolite:
    def test_a_rate_limit_stops_the_run_and_leaves_the_rest_for_next_time(self, tmp_path):
        calls = []

        def fetch(url):
            calls.append(url)
            raise hb.RateLimited(retry_after_s=0.0)
        r = hb.download_range(fetch, tmp_path, "XAUUSD", H0, H0 + 40 * 3600,
                              workers=1, max_rate_limited=3)
        assert r.rate_limited is True
        assert len(calls) <= 4
        assert r.fetched == 0
        assert not list(tmp_path.rglob("*.bi5"))

    def test_a_rate_limit_is_waited_out_then_the_hour_succeeds(self, tmp_path):
        state = {"n": 0}
        slept = []

        def fetch(url):
            state["n"] += 1
            if state["n"] == 1:
                raise hb.RateLimited(retry_after_s=7.0)
            return _blob([(0, 2000100, 2000000)])
        r = hb.download_range(fetch, tmp_path, "XAUUSD", H0, H0 + 3600, workers=1,
                              sleep=slept.append)
        assert r.fetched == 1 and not r.rate_limited
        assert 7.0 in slept

    def test_one_success_resets_the_run_of_rate_limits(self, tmp_path):
        seq = iter([hb.RateLimited(0.0), "ok", hb.RateLimited(0.0), "ok",
                    hb.RateLimited(0.0), "ok"])

        def fetch(url):
            v = next(seq)
            if isinstance(v, Exception):
                raise v
            return _blob([(0, 2000100, 2000000)])
        r = hb.download_range(fetch, tmp_path, "XAUUSD", H0, H0 + 3 * 3600, workers=1,
                              max_rate_limited=2, sleep=lambda s: None)
        assert r.fetched == 3 and not r.rate_limited
