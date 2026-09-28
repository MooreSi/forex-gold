"""The drawings API (docs/todo/011). Forwarding and shape checks only; storage
is tested in tests/market/test_chart_drawings.py. Nothing here reaches a broker:
the sentinel engine fails any test that touches it."""
from __future__ import annotations

import pytest

from backend.src.api.routers import chart_drawings as router_mod

TREND = {"symbol": "XAUUSD", "kind": "trend",
         "points": [{"time": 1_790_600_000, "price": 4150.5},
                    {"time": 1_790_603_600, "price": 4160.25}]}


@pytest.fixture
def ctl(monkeypatch):
    calls: list[tuple] = []
    ctl = router_mod.drawings_ctl

    async def _list(symbol):
        calls.append(("list", symbol))
        return [{"id": 1, "symbol": symbol, **{k: TREND[k] for k in ("kind", "points")},
                 "created_at": 1.0, "updated_at": 1.0}]

    async def _create(symbol, kind, points):
        calls.append(("create", symbol, kind, points))
        return {"id": 7, "symbol": symbol, "kind": kind, "points": points,
                "created_at": 1.0, "updated_at": 1.0}

    async def _update(drawing_id, points):
        calls.append(("update", drawing_id, points))
        if drawing_id != 7:
            return None
        return {"id": 7, "symbol": "XAUUSD", "kind": "trend", "points": points,
                "created_at": 1.0, "updated_at": 2.0}

    async def _delete(drawing_id):
        calls.append(("delete", drawing_id))
        return drawing_id == 7

    monkeypatch.setattr(ctl, "list_for", _list)
    monkeypatch.setattr(ctl, "create", _create)
    monkeypatch.setattr(ctl, "update_points", _update)
    monkeypatch.setattr(ctl, "delete", _delete)
    return calls


def test_lists_the_drawings_for_one_symbol(make_client, ctl):
    r = make_client().get("/api/chart/drawings?symbol=XAUUSD")
    assert r.status_code == 200
    assert r.json()[0]["points"] == TREND["points"]
    assert ctl == [("list", "XAUUSD")]


def test_saves_a_new_drawing(make_client, ctl):
    r = make_client().post("/api/chart/drawings", json=TREND)
    assert r.status_code == 200
    assert r.json()["id"] == 7
    assert ctl == [("create", "XAUUSD", "trend", TREND["points"])]


def test_moves_a_drawing(make_client, ctl):
    pts = [{"time": 1, "price": 2.0}, {"time": 3, "price": 4.0}]
    r = make_client().put("/api/chart/drawings/7", json={"points": pts})
    assert r.status_code == 200
    assert r.json()["points"] == pts


def test_deletes_a_drawing(make_client, ctl):
    assert make_client().delete("/api/chart/drawings/7").status_code == 200
    assert ctl == [("delete", 7)]


@pytest.mark.parametrize("call", [
    lambda c: c.put("/api/chart/drawings/8", json={"points": [{"time": 1, "price": 1.0}]}),
    lambda c: c.delete("/api/chart/drawings/8"),
])
def test_an_unknown_drawing_is_a_404(make_client, ctl, call):
    assert call(make_client()).status_code == 404


@pytest.mark.parametrize("body", [
    {**TREND, "kind": "arrow"},                                        # not a tool we have
    {**TREND, "points": TREND["points"][:1]},                          # a trend needs two
    {**TREND, "kind": "hline"},                                        # a level needs one
    {**TREND, "symbol": "XAU USD; DROP"},                              # not a symbol
    {**TREND, "points": [{"time": 1, "price": None}] * 2},             # a browser's NaN
])
def test_a_malformed_drawing_is_refused_before_storage(make_client, ctl, body):
    import json as _json
    r = make_client().post("/api/chart/drawings", content=_json.dumps(body, allow_nan=True),
                           headers={"content-type": "application/json"})
    assert r.status_code == 422
    assert ctl == []


def test_too_many_drawings_is_a_refusal_not_a_crash(make_client, monkeypatch):
    from backend.src.services.market.chart_drawings import TooManyDrawings

    async def _full(*_a):
        raise TooManyDrawings("full")

    monkeypatch.setattr(router_mod.drawings_ctl, "create", _full)
    assert make_client().post("/api/chart/drawings", json=TREND).status_code == 409


def test_a_literal_nan_price_is_never_stored(make_client, ctl):
    # Refused, but as a 500, not a 422: FastAPI's default validation handler
    # echoes the input back and NaN is not JSON. App-wide, not this route's;
    # a browser cannot send it (JSON.stringify(NaN) is null, covered above).
    import json as _json
    body = {**TREND, "points": [{"time": 1, "price": float("nan")}] * 2}
    r = make_client().post("/api/chart/drawings", content=_json.dumps(body, allow_nan=True),
                           headers={"content-type": "application/json"})
    assert r.status_code >= 400
    assert ctl == []


POSITION = {"symbol": "XAUUSD", "kind": "position",
            "points": [{"time": 1_790_600_000, "price": 4150.0},     # entry
                       {"time": 1_790_603_600, "price": 4140.0},     # stop
                       {"time": 1_790_603_600, "price": 4170.0}]}    # target


def test_a_position_is_saved_with_its_entry_stop_and_target(make_client, ctl):
    """The long/short position tool (2026-09-28): three prices, so the chart
    can turn it into an order without anyone retyping them."""
    r = make_client().post("/api/chart/drawings", json=POSITION)
    assert r.status_code == 200
    assert ctl == [("create", "XAUUSD", "position", POSITION["points"])]


def test_a_position_without_its_target_is_refused(make_client, ctl):
    r = make_client().post("/api/chart/drawings",
                           json={**POSITION, "points": POSITION["points"][:2]})
    assert r.status_code == 422
    assert ctl == []


def test_a_position_can_be_moved_with_all_three_of_its_points(make_client, ctl):
    r = make_client().put("/api/chart/drawings/7", json={"points": POSITION["points"]})
    assert r.status_code == 200
    assert r.json()["points"] == POSITION["points"]
