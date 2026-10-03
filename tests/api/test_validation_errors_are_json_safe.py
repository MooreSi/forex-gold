"""A request that fails validation is a 422, whatever it contained.

FastAPI's default handler echoes each error's `input` back in the body. A
float NaN or Infinity sent as a JSON literal (`json.dumps(..., allow_nan=True)`
writes `NaN`, and Starlette's parser accepts it) then cannot be serialised on
the way out, the catch-all in `errors.py` sees a ValueError, and a malformed
request is reported as a server fault with a correlation id.

No test here reaches a broker: the order routes are backed by the
`SentinelEngine` from tests/api/conftest.py, and every request is refused
before it gets that far.
"""
from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, field_validator

from backend.src.api import errors


def _post_literal(client, path: str, body: dict):
    return client.post(path, content=json.dumps(body, allow_nan=True),
                       headers={"content-type": "application/json"})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_literal_in_an_order_body_is_a_422(make_client, sentinel_engine, value):
    r = _post_literal(make_client(), "/api/trading/orders/market", {"direction": value})

    assert r.status_code == 422
    assert r.json()["error"]["kind"] == "invalid"
    assert sentinel_engine.calls == []


def test_a_non_finite_lot_size_on_partial_close_is_a_422(make_client, sentinel_engine):
    r = _post_literal(make_client(), "/api/trading/trades/T-9/partial-close",
                      {"lots_to_close": float("nan"), "close_price": 1.0})

    assert r.status_code == 422
    assert sentinel_engine.calls == []


def test_the_422_body_names_the_field_and_is_strict_json(make_client):
    r = _post_literal(make_client(), "/api/trading/orders/market", {"direction": float("nan")})

    # Strict: Python's json.loads would accept NaN; a browser's JSON.parse would not.
    body = json.loads(r.text, parse_constant=lambda c: pytest.fail(f"non-JSON constant {c}"))
    assert body["error"]["ref"] is None
    assert body["error"]["message"]
    assert any(e["loc"][-1] == "direction" for e in body["detail"])


class _PriceBody(BaseModel):
    price: float

    @field_validator("price")
    @classmethod
    def _positive(cls, v):
        if not v > 0:
            raise ValueError("price must be positive")
        return v


def _app_with_a_validator_that_raises():
    app = FastAPI()
    errors.install(app)

    @app.post("/thing")
    async def _thing(body: _PriceBody):  # noqa: ANN202
        return {"ok": True}

    return app


def test_a_validator_exception_in_ctx_does_not_become_a_500():
    """pydantic puts the raised ValueError object itself in the error's `ctx`.
    That is not JSON either."""
    client = TestClient(_app_with_a_validator_that_raises(), raise_server_exceptions=False)

    r = client.post("/thing", json={"price": -1.0})

    assert r.status_code == 422
    assert "price must be positive" in r.text


def test_a_nan_that_fails_a_validator_is_a_422_not_a_500():
    client = TestClient(_app_with_a_validator_that_raises(), raise_server_exceptions=False)

    r = _post_literal(client, "/thing", {"price": float("nan")})

    assert r.status_code == 422
    json.loads(r.text, parse_constant=lambda c: pytest.fail(f"non-JSON constant {c}"))
