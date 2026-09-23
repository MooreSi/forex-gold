"""Cross-asset features at a signal's creation. docs/todo/reversal-engine/230.

Pure arithmetic over candle lists. Nothing here reaches a broker.
"""
import json
import math

import pytest

from backend.src.services.reversal_engine import cross_asset as xa

_BAR = 300
_T = 1_790_000_100.0      # the signal's creation time


def _series(closes, end_open_ts=_T - _BAR):
    """Candles whose LAST bar opens at `end_open_ts` (so it closes at `_T`)."""
    n = len(closes)
    return [{"ts": end_open_ts - (n - 1 - i) * _BAR, "close": c}
            for i, c in enumerate(closes)]


def _walk(n=80, seed=1, start=100.0, step=0.002):
    """A deterministic zig-zag with drift so volatility is never zero."""
    out, x = [], start
    for i in range(n):
        x *= 1 + (step if (i * 7 + seed) % 3 else -step * 1.5)
        out.append(x)
    return out


class TestNoLookAhead:
    def test_a_bar_that_closes_after_the_signal_changes_nothing(self):
        gold = _series(_walk())
        peer = _series(_walk(seed=2))
        before = xa.peer_features(gold, peer, _T, "BUY")
        spike = peer + [{"ts": _T, "close": peer[-1]["close"] * 1.5}]
        assert xa.peer_features(gold, spike, _T, "BUY") == before


class TestDirection:
    def test_z60_flips_sign_with_the_trade_direction(self):
        gold = _series(_walk())
        peer = _series(_walk(seed=2))
        buy = xa.peer_features(gold, peer, _T, "BUY")
        sell = xa.peer_features(gold, peer, _T, "SELL")
        assert buy["z60"] == pytest.approx(-sell["z60"])
        assert buy["z15"] == pytest.approx(-sell["z15"])
        assert buy["z60"] != 0

    def test_a_rising_peer_is_positive_for_a_buy(self):
        rising = _series([100.0 * (1.001 ** i) + (0.01 if i % 2 else 0) for i in range(80)])
        f = xa.peer_features(_series(_walk()), rising, _T, "BUY")
        assert f["z60"] > 0


class TestFreshness:
    def test_a_peer_whose_last_bar_is_old_is_missing(self):
        stale = _series(_walk(seed=2), end_open_ts=_T - _BAR - 16 * 60)
        f = xa.peer_features(_series(_walk()), stale, _T, "BUY")
        assert all(v is None for v in f.values())

    def test_a_fresh_peer_is_present(self):
        f = xa.peer_features(_series(_walk()), _series(_walk(seed=2)), _T, "BUY")
        assert all(v is not None for v in f.values())

    def test_too_little_history_is_missing(self):
        f = xa.peer_features(_series(_walk()), _series(_walk(n=20)), _T, "BUY")
        assert f["corr"] is None


class TestCorrelation:
    def test_a_peer_that_copies_gold_is_plus_one(self):
        g = _walk()
        f = xa.peer_features(_series(g), _series([c * 0.5 for c in g]), _T, "BUY")
        assert f["corr"] == pytest.approx(1.0)

    def test_a_peer_that_mirrors_gold_is_minus_one(self):
        g = _walk()
        mirror = [100.0 * 100.0 / c for c in g]          # inverse moves
        f = xa.peer_features(_series(g), _series(mirror), _T, "BUY")
        assert f["corr"] == pytest.approx(-1.0, abs=0.01)

    def test_a_peer_stamped_seconds_off_the_bar_still_lines_up(self):
        """Found on the first real back-fill (2026-09-23): the bridge stamps
        each symbol's bars a few seconds off the boundary -- silver +1s, the
        S&P +9s, VIX +40s -- while gold's are exact. Matched on exact
        timestamps, VIX correlated on 0 of 6,570 signals."""
        g = _walk()
        peer = [{"ts": c["ts"] + 40, "close": c["close"] * 0.5} for c in _series(g)]
        f = xa.peer_features(_series(g), peer, _T + 40, "BUY")
        assert f["corr"] == pytest.approx(1.0)

    def test_a_flat_peer_has_no_correlation_not_zero(self):
        f = xa.peer_features(_series(_walk()), _series([50.0] * 80), _T, "BUY")
        assert f["corr"] is None


class TestVolatilityRatio:
    def test_a_quiet_hour_after_a_busy_morning_is_below_one(self):
        busy = _walk(n=68, step=0.004)
        quiet = [busy[-1] * (1 + (0.0005 if i % 2 else -0.0005)) for i in range(12)]
        f = xa.peer_features(_series(_walk()), _series(busy + quiet), _T, "BUY")
        assert f["vol_ratio"] < 1


class TestTheVector:
    def test_the_vector_has_four_features_per_peer_in_a_fixed_order(self):
        assert len(xa.FEATURE_NAMES) == 4 * len(xa.PEERS)
        assert xa.FEATURE_NAMES[:4] == [f"{xa.PEERS[0]}_z15", f"{xa.PEERS[0]}_z60",
                                        f"{xa.PEERS[0]}_vol_ratio", f"{xa.PEERS[0]}_corr"]

    def test_missing_values_take_their_neutral(self):
        vec = xa.vector({"v": 1, "features": {}})
        assert len(vec) == len(xa.FEATURE_NAMES)
        for name, v in zip(xa.FEATURE_NAMES, vec):
            assert v == (1.0 if name.endswith("vol_ratio") else 0.0)

    def test_a_row_never_measured_has_no_vector(self):
        assert xa.vector(None) is None
        assert xa.vector("not json") is None

    def test_a_stored_json_string_is_read(self):
        vec = xa.vector('{"v": 1, "features": {"XAGUSD_z60": 1.5}}')
        assert vec[xa.FEATURE_NAMES.index("XAGUSD_z60")] == 1.5

    def test_extreme_values_are_clamped(self):
        vec = xa.vector({"v": 1, "features": {"XAGUSD_z60": 40.0}})
        assert vec[xa.FEATURE_NAMES.index("XAGUSD_z60")] == xa.Z_CLAMP

    def test_snapshot_covers_every_peer(self):
        gold = _series(_walk())
        peers = {p: _series(_walk(seed=i + 2)) for i, p in enumerate(xa.PEERS)}
        snap = xa.snapshot(gold, peers, _T, "SELL")
        assert set(snap["features"]) == set(xa.FEATURE_NAMES)
        assert snap["v"] == xa.SCHEMA_VERSION
        assert not any(v is None for v in snap["features"].values())

    def test_a_peer_the_broker_did_not_send_is_missing_not_an_error(self):
        snap = xa.snapshot(_series(_walk()), {}, _T, "BUY")
        assert all(v is None for v in snap["features"].values())
        assert all(math.isfinite(x) for x in xa.vector(snap))


class TestTheDailyCorrelationSeries:
    """The chart's first panel: how closely each peer moved with gold, by day."""

    def _rec(self, t, **corrs):
        feats = {n: None for n in xa.FEATURE_NAMES}
        for peer, c in corrs.items():
            feats[f"{peer}_corr"] = c
        return {"created_at": t, "xasset_json": json.dumps({"v": 1, "features": feats})}

    def test_each_day_averages_each_peer_over_that_days_signals(self):
        day1 = 1_789_000_000.0          # 2026-09-10
        recs = [self._rec(day1, XAGUSD=0.8, USDX=-0.4),
                self._rec(day1 + 60, XAGUSD=0.6, USDX=-0.6),
                self._rec(day1 + 86_400, XAGUSD=0.2)]
        out = xa.daily_correlation(recs)
        assert [d["day"] for d in out] == ["2026-09-10", "2026-09-11"]
        assert out[0]["XAGUSD"] == pytest.approx(0.7)
        assert out[0]["USDX"] == pytest.approx(-0.5)
        assert out[0]["n"] == 2

    def test_a_peer_never_measured_that_day_is_absent_not_zero(self):
        out = xa.daily_correlation([self._rec(1_789_000_000.0, XAGUSD=0.5)])
        assert out[0]["USDX"] is None

    def test_an_unreadable_record_is_skipped(self):
        out = xa.daily_correlation([{"created_at": 1_789_000_000.0, "xasset_json": "{"}])
        assert out == []
