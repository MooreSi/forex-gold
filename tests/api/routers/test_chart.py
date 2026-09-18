"""The Chart router forwards; it does not compute.

Every assertion here is about what the handler *asked for*, not about what came
back. A test that only checks the response shape passes just as happily against
a handler that invented the numbers, and inventing numbers is the specific
failure mode this layer exists to prevent: the EMA the chart draws must be the
same `core_indicators.ema_series` the signal capture uses, or the two drift and
a screenshot stops being evidence about what the engine saw.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import chart as chart_router


def _candles(n: int) -> list[dict]:
    return [{"ts": float(1000 + i * 60), "open": 10.0 + i, "high": 11.0 + i,
             "low": 9.0 + i, "close": 10.5 + i} for i in range(n)]


def test_the_timeframe_selector_is_the_one_the_page_offered(make_client):
    r = make_client().get("/api/chart/timeframes")
    assert r.json()["timeframes"] == ["1m", "5m", "15m", "30m", "1H", "4H", "1D"]
    assert r.json()["ema_periods"] == [9, 21, 50]


def test_candles_are_fetched_with_the_mt5_name_for_the_requested_timeframe(
    make_client, sentinel_engine,
):
    """The browser says "15m"; the bridge only knows "M15". If the translation
    is skipped the bridge returns an empty list and the chart renders blank
    with no error anywhere."""
    sentinel_engine.candles = _candles(3)
    make_client().get("/api/chart/candles?timeframe=15m&count=120")
    args, _ = sentinel_engine.call_named("get_candles")
    assert args == ("M15", 120)


def test_an_unknown_timeframe_is_refused_by_name_not_forwarded(make_client, sentinel_engine):
    r = make_client().get("/api/chart/candles?timeframe=7y")
    assert r.status_code == 400
    assert "7y" in r.json()["error"]["message"]
    assert sentinel_engine.calls == [], "a bad timeframe reached the bridge"


def test_candle_fields_are_not_renamed_on_the_way_through(make_client, sentinel_engine):
    sentinel_engine.candles = _candles(2)
    rows = make_client().get("/api/chart/candles?timeframe=5m").json()
    assert rows[0] == {"ts": 1000.0, "open": 10.0, "high": 11.0,
                       "low": 9.0, "close": 10.5}


def test_overlays_use_the_controllers_indicator_maths_not_its_own(
    make_client, sentinel_engine, monkeypatch,
):
    """Pinned by substitution: if the handler computed an EMA itself, replacing
    the controller's function would not change the answer."""
    seen = {}

    def fake_ema(closes, period):
        seen.setdefault("ema", []).append((tuple(closes), period))
        return [float(period)] * len(closes)

    def fake_rsi(closes, period):
        seen["rsi"] = (tuple(closes), period)
        return [50.0] * len(closes)

    monkeypatch.setattr(chart_router.chart_ctl, "ema_series", fake_ema)
    monkeypatch.setattr(chart_router.chart_ctl, "rsi_series", fake_rsi)
    monkeypatch.setattr(chart_router.chart_ctl, "detect_fvgs", lambda rows: [])
    monkeypatch.setattr(chart_router.chart_ctl, "select_display_fvgs", lambda rows, f: [])

    sentinel_engine.candles = _candles(4)
    body = make_client().get("/api/chart/overlays?timeframe=5m").json()

    closes = (10.5, 11.5, 12.5, 13.5)
    assert seen["ema"] == [(closes, 9), (closes, 21), (closes, 50)]
    assert seen["rsi"] == (closes, 14)
    assert body["emas"]["21"] == [21.0] * 4
    assert body["rsi"] == [50.0] * 4


def test_overlays_and_candles_come_from_one_fetch_of_the_same_window(
    make_client, sentinel_engine, monkeypatch,
):
    """One endpoint for all three overlays, so they cannot disagree about which
    candles they were drawn on."""
    monkeypatch.setattr(chart_router.chart_ctl, "detect_fvgs", lambda rows: [])
    monkeypatch.setattr(chart_router.chart_ctl, "select_display_fvgs", lambda rows, f: [])
    sentinel_engine.candles = _candles(5)
    make_client().get("/api/chart/overlays?timeframe=1H&count=300")
    fetches = [c for c in sentinel_engine.calls if c[0] == "get_candles"]
    assert len(fetches) == 1
    assert fetches[0][1] == ("H1", 300)


def test_a_gap_carries_the_timestamp_of_the_candle_it_was_found_on(
    make_client, sentinel_engine, monkeypatch,
):
    """The detector reports an index; the browser plots against time. The
    resolution happens here so the index stays private to the pair that agree
    on what it means."""
    monkeypatch.setattr(chart_router.chart_ctl, "detect_fvgs", lambda rows: [])
    monkeypatch.setattr(
        chart_router.chart_ctl, "select_display_fvgs",
        lambda rows, f: [{"idx": 2, "top": 12.0, "bottom": 11.0, "direction": "up"}],
    )
    sentinel_engine.candles = _candles(5)
    body = make_client().get("/api/chart/overlays?timeframe=5m").json()
    assert body["fvgs"][0]["ts"] == 1120.0      # candle index 2


def test_an_out_of_range_gap_index_does_not_crash_the_overlay(
    make_client, sentinel_engine, monkeypatch,
):
    """Negative control on the lookup above. An index past the end of the
    window must leave the gap without a ts, not raise — a stale detector result
    should cost one undrawn zone, not the whole chart."""
    monkeypatch.setattr(chart_router.chart_ctl, "detect_fvgs", lambda rows: [])
    monkeypatch.setattr(
        chart_router.chart_ctl, "select_display_fvgs",
        lambda rows, f: [{"idx": 99, "top": 1.0, "bottom": 0.0, "direction": "up"}],
    )
    sentinel_engine.candles = _candles(3)
    r = make_client().get("/api/chart/overlays?timeframe=5m")
    assert r.status_code == 200
    assert r.json()["fvgs"] == [], "an unplaceable gap was returned instead of dropped"
    # And the rest of the payload survived — losing one zone must not cost the
    # EMAs and the RSI too.
    assert len(r.json()["rsi"]) == 3


def test_the_tick_endpoint_returns_null_rather_than_inventing_a_price(
    make_client, sentinel_engine,
):
    """A missing tick is missing data. A zero would render as a price."""
    sentinel_engine.tick = None
    assert make_client().get("/api/chart/tick").json() is None


def test_open_trades_go_through_the_controller_not_the_engine_directly(
    make_client, sentinel_engine, monkeypatch,
):
    called = {}

    async def fake(engine):
        called["engine"] = engine
        return [{"id": 1, "direction": "BUY", "entry": 2000.0}]

    monkeypatch.setattr(chart_router.chart_ctl, "get_open_trades", fake)
    rows = make_client().get("/api/chart/trades").json()
    assert called["engine"] is sentinel_engine
    assert rows[0]["direction"] == "BUY"


def test_no_chart_endpoint_accepts_a_write(make_client):
    """Read-only by construction. If a POST ever appears on this router it
    should be a deliberate decision, not a copy-paste."""
    client = make_client()
    for path in ("/api/chart/candles", "/api/chart/tick", "/api/chart/overlays",
                 "/api/chart/trades", "/api/chart/context"):
        assert client.post(path).status_code == 405, path


# ── System reads that belong to no single tab ────────────────────────────────

def test_the_release_list_is_served_with_the_running_version(make_client, monkeypatch):
    """The About tab shows both together, and a version that does not match the
    top of the changelog is the first sign of a bad build."""
    from backend.src.api.routers import system as system_router

    monkeypatch.setattr(system_router.system_ctl, "app_version", lambda: "1.4.2")
    monkeypatch.setattr(system_router.system_ctl, "releases",
                        lambda: [{"version": "1.4.2", "notes": ["Ported the News tab"]}])

    body = make_client().get("/api/system/releases").json()

    assert body["version"] == "1.4.2"
    assert body["releases"][0]["notes"] == ["Ported the News tab"]
