"""The money router.

**No test in this file may place, close or modify an order on any account,
live or demo.** The engine is `SentinelEngine` from `tests/api/conftest.py`:
it records calls and returns a canned dict. It holds no bridge, opens no
socket and imports no MT5 binding. `test_no_test_in_this_file_can_reach_a_real_engine`
asserts that rather than assuming it, in the style of
`tests/.../test_bridge_process_relocation.py::test_no_test_in_this_file_can_spawn_a_process`.

What these tests are actually for: proving the handler forwards the caller's
arguments **unchanged**. The close path is frozen by golden rule 2 — no
argument added, removed, reordered or defaulted — and an HTTP handler that
quietly supplied its own `reason`, clamped a lot size or reordered a positional
pair would be exactly the reshaping that rule forbids. A test that only checked
"a 200 came back" would not see any of it.
"""
from __future__ import annotations

import inspect

import pytest

from backend.src.api.routers import orders as orders_router


def test_no_test_in_this_file_can_reach_a_real_engine(sentinel_engine):
    """Guard rail, asserted rather than assumed."""
    assert not hasattr(sentinel_engine, "_bridge")
    assert type(sentinel_engine).__name__ == "SentinelEngine"
    assert type(sentinel_engine).__module__.endswith("conftest")


# ── the frozen close path ────────────────────────────────────────────────────

def test_close_forwards_the_trade_id_and_reason_positionally_in_that_order(
    make_client, sentinel_engine,
):
    r = make_client().post("/api/trading/trades/T-42/close",
                           json={"reason": "operator_closed"})
    assert r.status_code == 200
    args, kwargs = sentinel_engine.call_named("close_trade")
    assert args == ("T-42", "operator_closed")
    assert kwargs == {}


def test_close_uses_the_engines_own_default_reason_when_none_is_given(
    make_client, sentinel_engine,
):
    """`manual_close` is the engine's default and it is what the close record
    is tagged with. A different default here would silently relabel every
    close made from the dashboard."""
    make_client().post("/api/trading/trades/T-1/close", json={})
    args, _ = sentinel_engine.call_named("close_trade")
    assert args == ("T-1", "manual_close")


def test_the_routers_default_reason_is_the_engines_default_reason():
    """Pins the two together so a change to the engine signature cannot leave
    a stale literal behind in the API layer."""
    from backend.src.api.schemas.trading import CloseRequest
    from backend.src.runtime import TradingRuntime
    engine_default = inspect.signature(TradingRuntime.close_trade).parameters["reason"].default
    assert CloseRequest().reason == engine_default


def test_partial_close_forwards_four_positionals_in_the_engines_order(
    make_client, sentinel_engine,
):
    make_client().post(
        "/api/trading/trades/T-9/partial-close",
        json={"lots_to_close": 0.05, "close_price": 2431.55, "reason": "TP2"},
    )
    args, kwargs = sentinel_engine.call_named("partial_close_trade")
    assert args == ("T-9", 0.05, 2431.55, "TP2")
    assert kwargs == {}


def test_a_partial_close_of_zero_lots_is_rejected_before_it_reaches_the_engine(
    make_client, sentinel_engine,
):
    """Not a risk decision — a nonsense one. Zero lots is not a partial close,
    and the engine should not have to defend against the dashboard."""
    r = make_client().post("/api/trading/trades/T-9/partial-close",
                           json={"lots_to_close": 0, "close_price": 1.0})
    assert r.status_code == 422
    assert sentinel_engine.calls == []


# ── opening ──────────────────────────────────────────────────────────────────

def test_a_market_order_forwards_every_field_with_the_engines_keyword_names(
    make_client, sentinel_engine,
):
    make_client().post("/api/trading/orders/market", json={
        "direction": "SELL", "stop_loss": 2450.0, "lot_size": 0.02,
        "strategy": "scalp", "take_profit": 2400.0, "source_name": "orb_report",
    })
    args, kwargs = sentinel_engine.call_named("open_manual_market_order")
    assert args == ("SELL",)
    assert kwargs == {"stop_loss": 2450.0, "lot_size": 0.02, "strategy": "scalp",
                      "take_profit": 2400.0, "source_name": "orb_report"}


def test_an_omitted_stop_loss_stays_None_and_is_not_filled_in_here(
    make_client, sentinel_engine,
):
    """`stop_loss=None` means "let DPM compute an ATR stop". If this layer
    substituted a number, DPM would never run and every dashboard order would
    carry a stop the risk engine did not choose."""
    make_client().post("/api/trading/orders/market", json={"direction": "BUY"})
    _, kwargs = sentinel_engine.call_named("open_manual_market_order")
    assert kwargs["stop_loss"] is None
    assert kwargs["lot_size"] is None
    assert kwargs["source_name"] == "manual_market"


def test_a_direction_the_engine_does_not_accept_never_reaches_it(
    make_client, sentinel_engine,
):
    r = make_client().post("/api/trading/orders/market", json={"direction": "sideways"})
    assert r.status_code == 422
    assert sentinel_engine.calls == []


def test_a_limit_order_forwards_four_positionals_then_the_eight_targets(
    make_client, sentinel_engine,
):
    make_client().post("/api/trading/orders/limit", json={
        "direction": "BUY", "entry_low": 2400.0, "entry_high": 2405.0,
        "stop_loss": 2390.0, "tp1": 2420.0, "tp8": 2500.0, "notes": "zone",
    })
    args, kwargs = sentinel_engine.call_named("open_manual_limit_order")
    assert args == ("BUY", 2400.0, 2405.0, 2390.0)
    assert kwargs["tp1"] == 2420.0
    assert kwargs["tp8"] == 2500.0
    assert kwargs["tp4"] is None
    assert kwargs["notes"] == "zone"
    assert kwargs["lot_size"] is None


def test_opening_from_a_signal_forwards_the_override_and_the_age_multiplier(
    make_client, sentinel_engine,
):
    make_client().post("/api/trading/signals/S-7/open",
                       json={"lot_size_override": 0.03, "age_lot_mult": 0.5})
    args, kwargs = sentinel_engine.call_named("open_trade_from_signal")
    assert args == ("S-7",)
    assert kwargs == {"lot_size_override": 0.03, "age_lot_mult": 0.5}


def test_the_age_multiplier_defaults_to_one_so_an_omitted_field_cannot_shrink_a_trade(
    make_client, sentinel_engine,
):
    make_client().post("/api/trading/signals/S-7/open", json={})
    _, kwargs = sentinel_engine.call_named("open_trade_from_signal")
    assert kwargs["age_lot_mult"] == 1.0


# ── refusals ─────────────────────────────────────────────────────────────────

def test_a_refusal_reaches_the_client_verbatim(make_client, sentinel_engine):
    """Frontend conventions §8: "Surface a rejection verbatim. If the backend
    refuses an order, the user needs the real reason, not 'something went
    wrong'." """
    sentinel_engine.raises = ValueError(
        "DPM is disabled and no stop loss was given — set one or enable DPM."
    )
    r = make_client().post("/api/trading/orders/market", json={"direction": "BUY"})
    assert r.status_code == 409
    assert r.json()["error"]["kind"] == "refusal"
    assert r.json()["error"]["message"] == (
        "DPM is disabled and no stop loss was given — set one or enable DPM."
    )


def test_an_unexpected_error_does_not_leak_its_text_to_the_client(
    make_client, sentinel_engine,
):
    """Negative control for the test above. A refusal is meant for the user; an
    AttributeError is not, and its text names internals the user cannot act
    on."""
    sentinel_engine.raises = AttributeError("'NoneType' object has no attribute '_bridge'")
    r = make_client().post("/api/trading/orders/market", json={"direction": "BUY"})
    assert r.status_code == 500
    assert r.json()["error"]["kind"] == "internal"
    assert "_bridge" not in r.json()["error"]["message"]
    assert r.json()["error"]["ref"], "an internal error needs a ref to find it in the log"


# ── method safety ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/api/trading/orders/market",
    "/api/trading/orders/limit",
    "/api/trading/trades/T-1/close",
    "/api/trading/trades/T-1/partial-close",
    "/api/trading/signals/S-1/open",
    "/api/trading/signals/S-1/cancel",
])
def test_no_money_endpoint_is_reachable_by_GET(path, make_client, sentinel_engine):
    """A GET that opens a position is one browser prefetch away from an order
    nobody asked for."""
    assert make_client().get(path).status_code == 405
    assert sentinel_engine.calls == []


def test_every_route_on_the_money_router_is_a_post():
    """Structural, not per-path: a seventh endpoint added later is covered by
    this without anybody remembering to add a case above."""
    for route in orders_router.router.routes:
        assert route.methods == {"POST"}, f"{route.path} allows {route.methods}"


def test_an_unauthenticated_order_request_is_rejected(
    make_client, sentinel_engine, monkeypatch,
):
    """With the gate on, auto-login off, and no session. 401 JSON, not a
    redirect — see `backend/src/api/auth.py`.

    `auto_login_enabled` is forced here rather than read: it comes from the
    config file in the user data directory, so without this the test asserts
    something about whoever is running it. On this machine it happens to be on,
    and the first version of this test passed or failed by accident.
    """
    from backend.src.api import auth as gate
    monkeypatch.setattr(gate, "auto_login_enabled", lambda: False)
    r = make_client(auth=True).post("/api/trading/orders/market",
                                    json={"direction": "BUY"})
    assert r.status_code == 401
    assert sentinel_engine.calls == [], "an unauthenticated request reached the engine"
