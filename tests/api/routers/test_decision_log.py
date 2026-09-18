"""The Signal Decision Log's three endpoints.

**Recording only.** Nothing here places, closes or changes a trade, and the
controller is a recorder in every test — what is asserted is the shape the
browser is handed and the two claims that shape has to carry:

  * **`mean_r` survives as null.** `report()` returns None, never 0.0, for a
    variant that has scored nothing. Zero expectancy and no evidence are
    different statements, and anything in this layer that coerced one into the
    other would put `0.000` next to a variant nobody has any reason to trust.
  * **The backfill is a POST.** It writes rows. A GET that writes is a GET a
    browser prefetch, a link checker or a refresh can fire.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import decision_log as dl_router


@pytest.fixture
def log(monkeypatch):
    state = {
        "summary": {
            "total": 12, "executed": 5, "blocked": 7,
            "resolved": 3, "awaiting_outcome": 2,
            "observed": 10, "reconstructed": 2,
            "by_path": [{"path": "ime", "executed": 2, "blocked": 1},
                        {"path": "full", "executed": 3, "blocked": 6}],
            "top_reasons": [{"reason": "outside trading hours", "n": 4}],
        },
        "report": [
            {"variant": "champion", "is_champion": True, "n_taken": 3,
             "n_skipped": 1, "n_abstained": 0, "mean_r": 0.42, "net": 12.5},
            {"variant": "news_gate", "is_champion": False, "n_taken": 0,
             "n_skipped": 4, "n_abstained": 0, "mean_r": None, "net": 0.0},
        ],
        "added": 7,
        "calls": [],
    }

    monkeypatch.setattr(dl_router.tg_ctl, "decision_log_summary",
                        lambda: state["calls"].append("summary") or state["summary"])
    monkeypatch.setattr(dl_router.tg_ctl, "decision_log_report",
                        lambda: state["calls"].append("report") or state["report"])
    monkeypatch.setattr(dl_router.tg_ctl, "decision_log_backfill",
                        lambda: state["calls"].append("backfill") or state["added"])
    return state


class TestTheSummary:
    def test_it_is_forwarded_whole(self, make_client, log):
        body = make_client().get("/api/decision-log/summary").json()

        assert body == log["summary"]

    def test_an_empty_log_is_reported_as_empty_rather_than_refused(
        self, make_client, log,
    ):
        """Day one is the empty case, and the tab says "nothing recorded yet"
        from it. A 404 or a refusal here would read as a broken feature."""
        log["summary"] = {"total": 0, "executed": 0, "blocked": 0, "resolved": 0,
                          "awaiting_outcome": 0, "observed": 0,
                          "reconstructed": 0, "by_path": [], "top_reasons": []}

        res = make_client().get("/api/decision-log/summary")

        assert res.status_code == 200
        assert res.json()["total"] == 0


class TestTheVariantReport:
    def test_it_is_wrapped_in_an_object_not_returned_as_a_bare_list(
        self, make_client, log,
    ):
        """A top-level JSON array leaves nowhere to add a field later without
        breaking every reader."""
        body = make_client().get("/api/decision-log/report").json()

        assert isinstance(body, dict)
        assert body["variants"] == log["report"]

    def test_a_variant_with_no_evidence_keeps_its_null(self, make_client, log):
        """The one that matters. Coercing None to 0.0 anywhere on this path
        shows "no evidence" and "flat expectancy" identically."""
        body = make_client().get("/api/decision-log/report").json()

        no_evidence = next(v for v in body["variants"] if v["variant"] == "news_gate")
        assert no_evidence["mean_r"] is None

    def test_a_variant_with_evidence_keeps_its_number(self, make_client, log):
        """Negative control: a layer that dropped mean_r entirely would pass
        the test above."""
        body = make_client().get("/api/decision-log/report").json()

        champion = next(v for v in body["variants"] if v["is_champion"])
        assert champion["mean_r"] == pytest.approx(0.42)


class TestTheBackfill:
    def test_it_reports_how_many_were_genuinely_new(self, make_client, log):
        """Safe to press twice, which is only useful if the count says so."""
        body = make_client().post("/api/decision-log/backfill").json()

        assert body["added"] == 7
        assert "7 past decision" in body["note"]

    def test_pressing_it_again_says_there_was_nothing_to_do(self, make_client, log):
        log["added"] = 0

        body = make_client().post("/api/decision-log/backfill").json()

        assert body["added"] == 0
        assert "Nothing new to rebuild" in body["note"]

    def test_it_is_not_reachable_by_a_get(self, make_client, log):
        """It writes rows. A GET that writes is a GET a prefetch can fire."""
        res = make_client().get("/api/decision-log/backfill")

        assert res.status_code == 405
        assert log["calls"] == []


class TestReadingNeverWrites:
    def test_neither_readout_triggers_a_rebuild(self, make_client, log):
        client = make_client()
        client.get("/api/decision-log/summary")
        client.get("/api/decision-log/report")

        assert "backfill" not in log["calls"]
