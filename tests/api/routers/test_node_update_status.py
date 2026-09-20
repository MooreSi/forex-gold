"""`GET /api/node/update` -- is there a newer build, and what changed in it.

This endpoint answered 500 on every call where an update was actually
available, which is the only time it is asked to do anything. Three faults on
one line:

  * `summarise_changes(available)` passed the whole check dict as the first
    positional argument; the service takes `(local_sha, remote_sha)`.
  * it is a coroutine and was never awaited.
  * it returns `(bullets, error)`; the handler put that tuple where the
    screen expects a list of strings.

None of it showed with no update pending, because the call sits behind
`if available` and an install that is up to date never reaches it. Found by
calling every read endpoint on the running app, 2026-09-20.

A summary is a nicety, never a precondition for updating -- the service's own
docstring says so. So a summary that cannot be produced must still leave the
Update button usable, and these tests pin that.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import node as node_router


@pytest.fixture
def lab(monkeypatch):
    state = {
        "check": {
            "available": True,
            "local_sha": "aaaaaaa", "remote_sha": "bbbbbbb",
            "commits": [{"sha": "bbbbbbb", "short_sha": "bbbbbbb",
                         "summary": "Fix the thing"}],
            "error": None,
        },
        "summary": (["Faster startup", "Fixes a chart crash"], ""),
        "calls": [],
    }

    async def _check():
        return dict(state["check"])

    async def _summarise(local_sha, remote_sha, *a, **kw):
        state["calls"].append((local_sha, remote_sha))
        if isinstance(state["summary"], Exception):
            raise state["summary"]
        return state["summary"]

    monkeypatch.setattr(node_router.system_ctl, "check_for_update", _check)
    monkeypatch.setattr(node_router.system_ctl, "summarise_changes", _summarise)
    monkeypatch.setattr(node_router.system_ctl, "app_version", lambda: "0.5")
    return state


def test_an_available_update_is_reported_without_erroring(make_client, lab):
    """The whole bug: this answered 500 whenever there was an update."""
    r = make_client().get("/api/node/update")

    assert r.status_code == 200
    assert r.json()["update"]["available"] is True


def test_the_current_version_is_stated(make_client, lab):
    assert make_client().get("/api/node/update").json()["current"] == "0.5"


def test_the_summary_is_a_list_of_lines_not_a_tuple(make_client, lab):
    """The service returns `(bullets, error)`. Handing that straight to the
    screen puts an error string where the last bullet belongs."""
    body = make_client().get("/api/node/update").json()

    assert body["changes"] == ["Faster startup", "Fixes a chart crash"]


def test_the_two_commits_being_compared_reach_the_service(make_client, lab):
    make_client().get("/api/node/update")

    assert lab["calls"] == [("aaaaaaa", "bbbbbbb")]


def test_an_install_that_is_up_to_date_asks_for_no_summary(make_client, lab):
    """Summarising costs a paid model call. Nothing changed means nothing
    to summarise."""
    lab["check"] = {"available": False, "local_sha": "aaaaaaa",
                    "remote_sha": "aaaaaaa", "commits": [], "error": None}

    body = make_client().get("/api/node/update").json()

    assert body["changes"] == []
    assert lab["calls"] == []


class TestASummaryIsNeverAPrecondition:
    """The service's own docstring: callers must treat a summary as a
    nicety, never as a precondition for updating. An operator who cannot
    update because the AI provider is unconfigured is worse off than one
    who updates without a description of what changed."""

    def test_no_ai_provider_still_reports_the_update(self, make_client, lab):
        lab["summary"] = ([], "no AI provider configured (Settings > AI)")

        body = make_client().get("/api/node/update").json()

        assert body["update"]["available"] is True
        assert body["changes"] == []

    def test_the_reason_is_passed_on_rather_than_swallowed(self, make_client, lab):
        lab["summary"] = ([], "no AI provider configured (Settings > AI)")

        body = make_client().get("/api/node/update").json()

        assert "Settings > AI" in body["changes_error"]

    def test_a_summariser_that_raises_does_not_hide_the_update(self, make_client,
                                                               lab):
        lab["summary"] = RuntimeError("model timed out")

        body = make_client().get("/api/node/update").json()

        assert body["update"]["available"] is True
        assert body["changes"] == []
        assert "model timed out" in body["changes_error"]

    def test_the_raw_commit_subjects_survive_as_the_fallback(self, make_client,
                                                             lab):
        """What the screen shows when there is no summary: the commit
        subjects were always in the check payload."""
        lab["summary"] = ([], "no AI provider configured")

        body = make_client().get("/api/node/update").json()

        assert body["update"]["commits"][0]["summary"] == "Fix the thing"
