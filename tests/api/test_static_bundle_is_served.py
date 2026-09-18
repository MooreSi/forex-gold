"""Mount order, and the SPA fallback.

The bug this file exists to prevent: a catch-all route registered before the
routers answers `/api/anything` with `index.html` and a 200. Nothing errors.
The browser just receives HTML where it expected JSON, and every panel renders
empty — which reads as "the dashboard is broken", not as "the routes are in the
wrong order". It is a two-line mistake and it costs an afternoon.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def bundle(tmp_path):
    d = tmp_path / "dist"
    (d / "assets").mkdir(parents=True)
    (d / "index.html").write_text("<!doctype html><title>FOREX</title>", encoding="utf-8")
    (d / "assets" / "app.js").write_text("console.log('x')", encoding="utf-8")
    return d


def test_the_index_is_served_at_the_root(make_client, bundle):
    r = make_client(bundle_dir=bundle).get("/")
    assert r.status_code == 200
    assert "<!doctype html>" in r.text


def test_an_unknown_client_route_falls_back_to_the_index(make_client, bundle):
    """A refresh on /trading must not 404 — the route only exists in the
    browser's router."""
    r = make_client(bundle_dir=bundle).get("/trading")
    assert r.status_code == 200
    assert "<!doctype html>" in r.text


def test_a_real_asset_is_served_as_itself_not_as_the_index(make_client, bundle):
    r = make_client(bundle_dir=bundle).get("/assets/app.js")
    assert r.status_code == 200
    assert "console.log" in r.text


def test_an_unknown_api_path_is_404_json_not_the_index(make_client, bundle):
    """The mount-order bug, pinned. If this ever returns HTML, the fallback has
    been registered ahead of the routers."""
    r = make_client(bundle_dir=bundle).get("/api/no-such-thing")
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert r.json()["error"]["kind"] == "not_found"


def test_a_real_api_route_is_not_swallowed_by_the_fallback(make_client, bundle):
    """The mount-order bug's other half, and the one that actually bites.

    The 404 test above survives a mis-ordered mount, because the fallback
    refuses `/api/*` itself. What does NOT survive is a real endpoint: with the
    bundle mounted first, `/api/chart/timeframes` returns the index with a 200.
    Verified by planting that exact mutation and watching this go red.
    """
    r = make_client(bundle_dir=bundle).get("/api/chart/timeframes")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json"), (
        "an /api route returned HTML — the bundle is mounted ahead of the routers"
    )
    assert "5m" in r.json()["timeframes"]


def test_the_app_still_starts_when_the_bundle_is_missing(make_client, tmp_path):
    """A source checkout has no dist/. It must start and say what to run, not
    crash on a StaticFiles mount that cannot find its directory."""
    client = make_client(bundle_dir=tmp_path / "absent")
    assert client.get("/healthz").status_code == 200
    r = client.get("/")
    assert r.status_code == 503
    assert "npm run build" in r.json()["error"]["message"]


def test_the_api_still_answers_when_the_bundle_is_missing(make_client, tmp_path):
    """The missing-bundle fallback must not swallow the API either — otherwise
    a developer with no dist/ cannot debug the endpoints."""
    r = make_client(bundle_dir=tmp_path / "absent").get("/api/chart/timeframes")
    assert r.status_code == 200
    assert "5m" in r.json()["timeframes"]
