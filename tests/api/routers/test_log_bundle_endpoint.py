"""Downloading the filtered logs.

Builds a bundle and hands it to the caller. It sends nothing anywhere -- the
NiceGUI original mailed it to a hardcoded address, and a browser download
needs no email provider configured and puts nobody's address in the code.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import log_bundle as router_mod


@pytest.fixture
def lab(monkeypatch):
    state = {
        "calls": [],
        "out": {
            "filename": "forex_trader_logs_20260920_0900.txt",
            "text": "FOREX Trader — Filtered Log Export\nsomething broke\n",
            "raw_lines": 12_000, "kept_lines": 42, "truncated": False, "note": "",
        },
    }

    def _build(days):
        state["calls"].append(days)
        return dict(state["out"])

    monkeypatch.setattr(router_mod.bundle_ctl, "build_log_bundle", _build)
    return state


def test_the_bundle_comes_back_as_text(make_client, lab):
    r = make_client().get("/api/settings/log-bundle")

    assert r.status_code == 200
    assert "something broke" in r.text


def test_it_is_offered_as_a_download_with_a_dated_name(make_client, lab):
    """Two exports from one machine must not overwrite each other."""
    r = make_client().get("/api/settings/log-bundle")

    assert "attachment" in r.headers["content-disposition"]
    assert "forex_trader_logs_20260920_0900.txt" in r.headers["content-disposition"]


def test_the_window_reaches_the_service(make_client, lab):
    make_client().get("/api/settings/log-bundle?days=14")

    assert lab["calls"] == [14]


def test_the_default_window_is_the_last_five_days(make_client, lab):
    make_client().get("/api/settings/log-bundle")

    assert lab["calls"] == [5]


def test_how_much_was_kept_is_readable_without_parsing_the_file(make_client, lab):
    r = make_client().get("/api/settings/log-bundle")

    assert r.headers["x-log-lines-kept"] == "42"
    assert r.headers["x-log-lines-scanned"] == "12000"
    assert r.headers["x-log-truncated"] == "0"


def test_a_truncated_bundle_says_so_in_a_header(make_client, lab):
    lab["out"] = {**lab["out"], "truncated": True}

    r = make_client().get("/api/settings/log-bundle")

    assert r.headers["x-log-truncated"] == "1"


def test_an_absurd_window_is_refused_rather_than_read(make_client, lab):
    """Otherwise one request reads every rotated file on disk."""
    r = make_client().get("/api/settings/log-bundle?days=4000")

    assert r.status_code == 400
    assert lab["calls"] == []


def test_a_nonsense_window_is_refused(make_client, lab):
    assert make_client().get("/api/settings/log-bundle?days=0").status_code == 422
    assert lab["calls"] == []
