"""The EA badge's popup: what is stale, and the button that fixes it.

The EA badge has shown "EA STALE BUILD" since 2026-09-09, and that is where
it stopped: an operator who saw it still had to find `tools/deploy_ea.sh`, run
it in a terminal, then compile. Asked for on 2026-09-20 -- click the badge,
see what is wrong, press one button.

What the button can honestly do depends on the machine, and this is the part
worth pinning:

  * a **pre-compiled .ex5 committed in mql5/** is copied straight into every
    terminal and the attached EA reloads it by itself -- no MetaEditor at all;
  * with only the .mq5, the source is copied and the operator is told to press
    F7, because MetaEditor's /compile does nothing under CrossOver (it exits
    0, writes no log and rebuilds nothing -- `ea_deploy.compile_ea` refuses on
    macOS for exactly that reason);
  * on Windows the compile runs here.

The endpoint must never claim a compile it did not do. That is the silent
staleness this whole area exists to end.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import settings as settings_router


@pytest.fixture
def lab(monkeypatch):
    state = {
        "stale": True,
        "detail": "The EA on the chart is v1.08, but its source has been edited.",
        "report": {"targets": ["/t1"], "deployed": 1, "already_current": 0,
                   "needs_compile": 1, "errors": []},
        "compile": {"ok": False, "detail": "compiling is Windows-only"},
        "compiled_calls": 0,
        "has_ex5": False,
    }

    def _compile(experts_dir, platform=None):
        state["compiled_calls"] += 1
        return dict(state["compile"])

    monkeypatch.setattr(settings_router.broker_ctl, "ea_build_status",
                        lambda: (state["stale"], state["detail"]))
    monkeypatch.setattr(settings_router.broker_ctl, "ea_deploy_report",
                        lambda: dict(state["report"]))
    monkeypatch.setattr(settings_router.broker_ctl, "ea_compile", _compile)
    monkeypatch.setattr(settings_router.broker_ctl, "ea_binary_is_shipped",
                        lambda: state["has_ex5"])
    return state


class TestWhatTheDialogReads:
    def test_it_reports_whether_the_build_is_stale(self, make_client, lab):
        body = make_client().get("/api/settings/ea").json()

        assert body["stale"] is True

    def test_it_carries_the_detail_the_badge_shows(self, make_client, lab):
        body = make_client().get("/api/settings/ea").json()

        assert "edited" in body["detail"]

    def test_it_says_whether_a_compiled_build_is_shipped(self, make_client, lab):
        """The whole difference between one click and one click plus F7."""
        lab["has_ex5"] = True

        assert make_client().get("/api/settings/ea").json()["binary_shipped"] is True

    def test_reading_it_installs_nothing(self, make_client, lab):
        make_client().get("/api/settings/ea")

        assert lab["compiled_calls"] == 0


class TestInstalling:
    def test_it_copies_the_ea_into_every_terminal(self, make_client, lab):
        body = make_client().post("/api/settings/ea/install", json={}).json()

        assert body["report"]["deployed"] == 1

    def test_a_shipped_binary_needs_no_compile_step(self, make_client, lab):
        """The .ex5 is already built, so nothing is asked of the operator."""
        lab["has_ex5"] = True
        lab["report"] = {"targets": ["/t1"], "deployed": 1, "already_current": 0,
                         "needs_compile": 0, "errors": []}

        body = make_client().post("/api/settings/ea/install", json={}).json()

        assert body["needs_compile"] is False
        assert lab["compiled_calls"] == 0

    def test_without_a_binary_it_asks_for_the_compile_rather_than_faking_it(
        self, make_client, lab,
    ):
        body = make_client().post("/api/settings/ea/install", json={}).json()

        assert body["needs_compile"] is True
        assert "F7" in body["next_step"]

    def test_a_failed_compile_is_never_reported_as_success(self, make_client, lab):
        """MetaEditor exits 0 on a build it never performed. An 'ok' here
        would be the silent staleness this area exists to end."""
        lab["report"]["needs_compile"] = 1
        lab["compile"] = {"ok": False, "detail": "nothing was rebuilt"}

        body = make_client().post("/api/settings/ea/install", json={}).json()

        assert body["compiled"] is False
        assert "nothing was rebuilt" in body["next_step"]

    def test_a_terminal_that_could_not_be_written_is_reported(self, make_client,
                                                              lab):
        lab["report"]["errors"] = ["/t2: permission denied"]

        body = make_client().post("/api/settings/ea/install", json={}).json()

        assert "permission denied" in " ".join(body["report"]["errors"])

    def test_finding_no_terminal_at_all_refuses_rather_than_claiming_success(
        self, make_client, lab,
    ):
        """"Installed into 0 terminals" is a no-op dressed as a fix."""
        lab["report"] = {"targets": [], "deployed": 0, "already_current": 0,
                         "needs_compile": 0, "errors": []}

        r = make_client().post("/api/settings/ea/install", json={})

        assert r.status_code >= 400
        assert "no metatrader" in r.json()["error"]["message"].lower()
