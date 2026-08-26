"""The signal-snapshot research log.

`backend/src/services/positions/core_signal_snapshot.py` was 11.9% covered --
118 of its 134 statements never executed by a test -- and it is one of the
modules dragging `services/positions` below its coverage floor after the
2026-08-25 merge.

It is also the kind of module where a bug hides indefinitely. Its own docstring
says so: *"Nothing here is on the trading path and every failure is swallowed:
a research log must never be able to stop a trade."* Swallowing failures is the
right call for a research feature, and it means a broken capture looks exactly
like a quiet market. The only thing that can tell them apart is a test.

Nothing here touches a broker. `_tf_indicators` takes whatever object it is
given and calls `get_candles` on it, so the double below is a SimpleNamespace
with one async attribute -- deliberately not a `_FakeBridge` class, because the
fixture-dedup ratchet counts those and is already over its baseline, and this
needs one method rather than a bridge surface.
"""
from __future__ import annotations

import asyncio
import types

import pytest

from backend.src.services.positions import core_signal_snapshot as snap


def _candles(n: int, *, closes=None, volumes=None) -> list[dict]:
    """n OHLC bars, rising by default so the EMAs stack bullish."""
    out = []
    for i in range(n):
        c = float(closes[i]) if closes else 100.0 + i
        v = float(volumes[i]) if volumes else 1000.0
        out.append({"open": c - 0.5, "high": c + 1.0, "low": c - 1.0, "close": c, "volume": v})
    return out


def _bridge(candles=None, raises=False):
    async def get_candles(tf, count):
        if raises:
            raise RuntimeError("bridge down")
        return candles
    return types.SimpleNamespace(get_candles=get_candles)


# ── _stage_for ────────────────────────────────────────────────────────────────
#
# The two-stage split this module exists to measure: VIP fires a bare market
# call, then sends levels ~40s later.

def test_a_row_with_no_levels_is_a_market_call():
    assert snap._stage_for({"status": "new"}) == "market_call"
    assert snap._stage_for({}) == "market_call"


def test_a_followup_or_instant_row_with_levels_is_the_levels_stage():
    assert snap._stage_for({"entry_low": 2400.0, "status": "followup"}) == "levels"
    assert snap._stage_for({"stop_loss": 2390.0, "status": "INSTANT"}) == "levels"


def test_a_row_that_arrived_complete_is_neither():
    """A channel that sends everything at once never had two stages."""
    assert snap._stage_for({"entry_high": 2405.0, "status": "new"}) == "complete"


def test_any_one_level_is_enough_to_count_as_having_levels():
    """entry_low OR entry_high OR stop_loss -- pinned because a partial fill
    of those three still means the follow-up arrived."""
    for field in ("entry_low", "entry_high", "stop_loss"):
        assert snap._stage_for({field: 1.0, "status": "followup"}) == "levels"


# ── _tf_indicators ────────────────────────────────────────────────────────────

def test_indicators_are_none_when_the_bridge_fails():
    """A broker hiccup must produce no row rather than a half-filled one."""
    assert asyncio.run(snap._tf_indicators(_bridge(raises=True), "M5")) is None


def test_indicators_are_none_without_enough_history():
    """Fewer than 30 bars cannot settle EMA50/RSI14/ATR14, so the module
    declines rather than recording numbers that mean nothing."""
    assert asyncio.run(snap._tf_indicators(_bridge(_candles(29)), "M5")) is None
    assert asyncio.run(snap._tf_indicators(_bridge([]), "M5")) is None
    assert asyncio.run(snap._tf_indicators(_bridge(None), "M5")) is None


def test_indicators_come_back_for_a_full_series():
    out = asyncio.run(snap._tf_indicators(_bridge(_candles(120)), "M5"))
    assert out is not None
    for key in ("close", "ema9", "ema21", "ema50", "ema_stack", "rsi14",
                "volume", "volume_avg20", "volume_ratio"):
        assert key in out, f"{key} missing from the snapshot row"
    assert out["close"] == 219.0          # 100.0 + 119, rounded to 2dp


def test_ema_stack_reads_bull_bear_and_mixed():
    """The verdict is recorded alongside the values so later analysis need not
    re-derive it -- which means the verdict itself has to be right."""
    rising = asyncio.run(snap._tf_indicators(_bridge(_candles(120)), "M5"))
    assert rising["ema_stack"] == "bull"

    falling = asyncio.run(snap._tf_indicators(
        _bridge(_candles(120, closes=[300.0 - i for i in range(120)])), "M5"))
    assert falling["ema_stack"] == "bear"

    flat = asyncio.run(snap._tf_indicators(
        _bridge(_candles(120, closes=[100.0] * 120)), "M5"))
    assert flat["ema_stack"] == "mixed"


def test_volume_ratio_compares_the_last_bar_to_the_recent_average():
    """>1 means this bar traded heavier than the recent norm."""
    vols = [1000.0] * 119 + [3000.0]
    out = asyncio.run(snap._tf_indicators(_bridge(_candles(120, volumes=vols)), "M5"))
    # last 20 bars: nineteen at 1000 plus one at 3000 -> avg 1100
    assert out["volume"] == 3000.0
    assert out["volume_avg20"] == 1100.0
    assert out["volume_ratio"] == round(3000.0 / 1100.0, 3)


def test_volume_ratio_is_none_rather_than_a_division_by_zero():
    out = asyncio.run(snap._tf_indicators(
        _bridge(_candles(120, volumes=[0.0] * 120)), "M5"))
    assert out["volume_ratio"] is None


def test_indicators_survive_an_indicator_library_failure():
    """compute_atr/compute_adx are wrapped: if they raise, the row still goes
    out with the rest of the picture and those two fields empty. A research log
    that drops the whole sample because one indicator failed is worse than one
    that records what it had."""
    import backend.src.services.dpm.engine as dpm

    def _boom(*a, **k):
        raise ValueError("bad series")

    original = dpm.compute_atr
    dpm.compute_atr = _boom
    try:
        out = asyncio.run(snap._tf_indicators(_bridge(_candles(120)), "M5"))
    finally:
        dpm.compute_atr = original

    assert out is not None
    assert out["atr14"] is None and out["adx14"] is None
    assert out["rsi14"] is not None, "the rest of the row should still be filled"


def test_the_timeframes_and_depth_are_what_the_indicators_need():
    """EMA50 needs well over 50 bars to settle; the constant is the reason the
    <30 guard above is not the only protection."""
    assert snap._TFS == ("M1", "M5", "M15")
    assert snap._CANDLES_PER_TF >= 60


# ── capture_snapshot ──────────────────────────────────────────────────────────
#
# pro_snapshots lives in the reversal engine's own namespaced connection (see
# backend/src/db/connection.py), so fresh_db alone does not create it -- the
# repo needs pointing at a temp file of its own.


@pytest.fixture
def pro_corpus_db(tmp_path):
    from backend.src.services.reversal_engine import pro_corpus
    from backend.src.services.reversal_engine import reversal_engine_repo as re_repo

    # Mirrors real startup: backend/src/app.py opens the reversal-engine db and
    # then calls pro_corpus.init(), which is what creates pro_snapshots.
    re_repo.init(str(tmp_path / "reversal.db"))
    pro_corpus.init()
    return tmp_path


def _signal_row(**over):
    row = {
        "tg_message_id": "m-1",
        "group_name": "GOLD DIGGERS VIP",
        "direction": "BUY",
        "parsed_at": 1000.0,
        "status": "new",
        "entry_low": None, "entry_high": None,
        "stop_loss": None, "tp1": None, "raw_text": "buy gold now",
    }
    row.update(over)
    return row


def _tick(bid=2400.0, ask=2400.4, spread_points=40.0):
    return types.SimpleNamespace(bid=bid, ask=ask, spread_points=spread_points)


def _full_bridge(tick=None, candles=None):
    async def get_tick():
        if tick is None:
            raise RuntimeError("no tick")
        return tick

    async def get_candles(tf, count):
        return candles if candles is not None else _candles(120)

    return types.SimpleNamespace(get_tick=get_tick, get_candles=get_candles)


def _stored(tmp_path):
    from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
    rows = re_repo.get_db().all("SELECT * FROM pro_snapshots")
    return [dict(r) for r in rows]


def test_capture_writes_one_row_and_records_the_lag_openly(fresh_db, pro_corpus_db, monkeypatch):
    """The module's design note says capture lag is "recorded explicitly rather
    than hidden". That is only true if the number is right."""
    monkeypatch.setattr(snap.time, "time", lambda: 1012.5)

    ok = asyncio.run(snap.capture_snapshot(_full_bridge(_tick()), _signal_row()))

    assert ok is True
    rows = _stored(pro_corpus_db)
    assert len(rows) == 1
    assert rows[0]["capture_lag_s"] == 12.5      # 1012.5 captured - 1000.0 signal
    assert rows[0]["stage"] == "market_call"


def test_a_second_capture_of_the_same_stage_is_refused(fresh_db, pro_corpus_db):
    """The poller can race itself; the UNIQUE constraint is what makes that
    safe, and capture_snapshot reports the refusal rather than claiming success."""
    bridge = _full_bridge(_tick())
    first = asyncio.run(snap.capture_snapshot(bridge, _signal_row()))
    second = asyncio.run(snap.capture_snapshot(bridge, _signal_row()))

    assert first is True
    assert second is False, "a duplicate (message, stage) must not report success"
    assert len(_stored(pro_corpus_db)) == 1


def test_the_same_message_at_a_later_stage_is_a_new_row(fresh_db, pro_corpus_db):
    """The two-stage split is the point of the module: the market call and the
    follow-up with levels are different rows for the same message."""
    bridge = _full_bridge(_tick())
    asyncio.run(snap.capture_snapshot(bridge, _signal_row()))
    asyncio.run(snap.capture_snapshot(
        bridge, _signal_row(entry_low=2399.0, entry_high=2401.0, status="followup")))

    stages = sorted(r["stage"] for r in _stored(pro_corpus_db))
    assert stages == ["levels", "market_call"]


def test_distance_to_the_zone_is_only_recorded_when_a_zone_was_given(fresh_db, pro_corpus_db):
    """A bare market call has no zone to be far from, so the field stays empty
    rather than being measured against live price and read as zero drift."""
    asyncio.run(snap.capture_snapshot(_full_bridge(_tick()), _signal_row()))
    assert _stored(pro_corpus_db)[0]["dist_to_entry_mid"] is None


def test_distance_to_the_zone_is_measured_from_the_zone_mid(fresh_db, pro_corpus_db):
    """bid 2400.0 / ask 2400.4 -> price 2400.2; zone 2398-2402 -> mid 2400."""
    asyncio.run(snap.capture_snapshot(
        _full_bridge(_tick()),
        _signal_row(entry_low=2398.0, entry_high=2402.0, status="followup")))
    assert _stored(pro_corpus_db)[0]["dist_to_entry_mid"] == pytest.approx(0.2, abs=0.01)


def test_a_dead_tick_does_not_stop_the_capture(fresh_db, pro_corpus_db):
    """Every failure here is swallowed by design -- a research log must never be
    able to stop a trade -- so a bridge with no tick still produces a row, with
    the price fields empty rather than the sample lost."""
    ok = asyncio.run(snap.capture_snapshot(_full_bridge(tick=None), _signal_row()))

    assert ok is True
    row = _stored(pro_corpus_db)[0]
    assert row["bid"] == 0 and row["ask"] == 0
    assert row["indicators_json"], "the candle-derived half should still be there"


def test_background_rows_are_captured_in_both_directions(fresh_db, pro_corpus_db):
    """FVG context is direction-relative, so a direction-less negative sample
    could not be compared against a directional signal."""
    n = asyncio.run(snap.capture_background_snapshot(_full_bridge(_tick())))

    assert n == 2
    rows = _stored(pro_corpus_db)
    assert sorted(r["direction"] for r in rows) == ["BUY", "SELL"]
    assert {r["stage"] for r in rows} == {"background"}
    assert {r["group_name"] for r in rows} == {"_BACKGROUND"}

