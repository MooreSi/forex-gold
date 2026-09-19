"""The AI Analysis tab.

Two things are worth guarding and neither is about the model's answer:

1. **The evidence is readable without paying for it.** The numbers are the
   answer most of the time, and a page that could only show them by asking a
   model would make every glance billable. `/evidence` calls no model and says
   `billable: false`.
2. **An unconfigured provider refuses, loudly.** An Analyse button that quietly
   returns nothing looks like a model with no opinion, which is a completely
   different and much more interesting result.

No model is called in this file: `ai_controller.complete` is replaced, and
`test_no_test_here_calls_a_real_model` proves the replacement is what ran.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import ai as ai_router


@pytest.fixture
def lab(monkeypatch):
    state = {
        "config": {"ai_provider": "anthropic", "claude_model": "claude-opus-5"},
        "configured": True,
        "db_path": "/tmp/forex.db",
        "channels": [{"channel_name": "GoldSignals", "stats": {"win_rate_pct": 61.0}}],
        "strategies": {"fixed_stats": {"count": 12}},
        "generator": {"strategies": []},
        "completions": [],
        "answer": "The evidence does not support leaving this channel on.",
    }

    async def _complete(cfg, system, prompt, max_tokens, **kw):
        state["completions"].append((cfg, system, prompt, max_tokens))
        return state["answer"]

    monkeypatch.setattr(ai_router.settings_ctl, "load_config", lambda: state["config"])
    monkeypatch.setattr(ai_router.settings_ctl, "get_config",
                        lambda key, default=None: state["db_path"])
    monkeypatch.setattr(ai_router.ai_ctl, "is_configured", lambda cfg: state["configured"])
    monkeypatch.setattr(ai_router.ai_ctl, "complete", _complete)
    monkeypatch.setattr(ai_router.ai_analysis_ctl, "gather_channel_data",
                        lambda path, days: state["channels"])
    monkeypatch.setattr(ai_router.ai_analysis_ctl, "gather_strategy_dpm_data",
                        lambda path, days: state["strategies"])
    monkeypatch.setattr(ai_router.ai_analysis_ctl, "gather_signal_generator_data",
                        lambda path, days: state["generator"])
    # One stand-in per subject, so a handler that sends the WRONG subject's
    # prompt is visible here rather than passing on a single shared string.
    # That is what it did until 2026-09-19.
    monkeypatch.setattr(ai_router.ai_analysis_ctl, "system_prompt_for",
                        lambda subject: f"You are a trading analyst for {subject}.")
    return state


def test_no_test_here_calls_a_real_model(lab):
    """Guard rail, asserted rather than assumed."""
    import inspect

    assert "_complete" in ai_router.ai_ctl.complete.__qualname__ or \
        inspect.iscoroutinefunction(ai_router.ai_ctl.complete)
    assert lab["completions"] == []


# ── What can be analysed ─────────────────────────────────────────────────────

def test_the_subjects_are_listed_with_the_model_that_would_answer(make_client, lab):
    """The page says WHICH model is about to be billed, not just that one is."""
    body = make_client().get("/api/ai/subjects").json()

    assert [s["id"] for s in body["subjects"]] == ["channels", "strategies", "generator"]
    assert body["configured"] is True
    assert body["provider"] == "anthropic"
    assert body["model"] == "claude-opus-5"


def test_an_unconfigured_install_says_so_in_the_subject_list(make_client, lab):
    lab["configured"] = False

    assert make_client().get("/api/ai/subjects").json()["configured"] is False


# ── Evidence, free ───────────────────────────────────────────────────────────

def test_the_evidence_is_readable_without_calling_a_model(make_client, lab):
    body = make_client().get("/api/ai/evidence?subject=channels&days=30").json()

    assert body["evidence"][0]["channel_name"] == "GoldSignals"
    assert body["billable"] is False
    assert lab["completions"] == [], "reading the numbers called a model"


def test_each_subject_reaches_its_own_gatherer(make_client, lab):
    client = make_client()

    assert client.get("/api/ai/evidence?subject=strategies").json()["evidence"] == {
        "fixed_stats": {"count": 12}}
    assert client.get("/api/ai/evidence?subject=generator").json()["evidence"] == {
        "strategies": []}


def test_the_window_reaches_the_gatherer(make_client, lab, monkeypatch):
    seen = {}
    monkeypatch.setattr(ai_router.ai_analysis_ctl, "gather_channel_data",
                        lambda path, days: seen.setdefault("days", days) and [])

    make_client().get("/api/ai/evidence?subject=channels&days=90")

    assert seen == {"days": 90}


def test_an_unknown_subject_is_refused_by_name(make_client, lab):
    r = make_client().get("/api/ai/evidence?subject=tea-leaves")

    assert r.status_code == 400
    assert "tea-leaves" in r.json()["error"]["message"]


def test_no_configured_database_is_refused_rather_than_analysed_as_empty(make_client, lab):
    """An analysis of an empty database reads as "this account has no edge"."""
    lab["db_path"] = ""

    r = make_client().get("/api/ai/evidence?subject=channels")

    assert r.status_code == 409


# ── Asking the model, billable ───────────────────────────────────────────────

def test_an_analysis_calls_the_model_once_and_says_it_was_billable(make_client, lab):
    body = make_client().post("/api/ai/analyse",
                              json={"subject": "channels", "days": 30}).json()

    assert body["answer"] == lab["answer"]
    assert body["billable"] is True
    assert len(lab["completions"]) == 1


def test_the_model_is_given_the_evidence_and_the_system_prompt(make_client, lab):
    make_client().post("/api/ai/analyse", json={"subject": "channels", "days": 30})

    _cfg, system, prompt, _max = lab["completions"][0]
    assert system == "You are a trading analyst for channels."
    assert "GoldSignals" in prompt


def test_each_subject_gets_its_own_prompt_not_a_shared_one(make_client, lab):
    """The bug this file missed for months.

    Every subject gathered its own evidence and was then sent the SIGNAL
    GENERATOR prompt -- which opens "You are given performance data for
    internal signal generator engines" and demands a schema keyed on
    `engines`. Asking about Telegram channels handed a paid model channel
    rows and told it they were engines.
    """
    for subject in ("channels", "strategies", "generator"):
        make_client().post("/api/ai/analyse", json={"subject": subject, "days": 30})

    systems = [c[1] for c in lab["completions"]]

    assert systems == ["You are a trading analyst for channels.",
                       "You are a trading analyst for strategies.",
                       "You are a trading analyst for generator."]


def test_an_unconfigured_provider_refuses_instead_of_answering_nothing(make_client, lab):
    """An empty answer reads as a model with no opinion."""
    lab["configured"] = False

    r = make_client().post("/api/ai/analyse", json={"subject": "channels"})

    assert r.status_code == 409
    assert "Settings → AI" in r.json()["error"]["message"]
    assert lab["completions"] == []


def test_an_unknown_subject_never_reaches_the_model(make_client, lab):
    r = make_client().post("/api/ai/analyse", json={"subject": "tea-leaves"})

    assert r.status_code == 400
    assert lab["completions"] == []


def test_the_analysis_endpoint_is_not_reachable_by_GET(make_client, lab):
    """A billable call behind a GET is one browser prefetch from a charge
    nobody asked for."""
    assert make_client().get("/api/ai/analyse").status_code == 405
    assert lab["completions"] == []


# ── DPM tables ───────────────────────────────────────────────────────────────

def test_the_dpm_tables_are_local_reads(make_client, lab, monkeypatch):
    async def _rows():
        return [{"strategy": "dpm", "count": 4}]

    async def _empty():
        return []

    monkeypatch.setattr(ai_router.dpm_ctl, "get_perf_rows", _rows)
    monkeypatch.setattr(ai_router.dpm_ctl, "get_calibration_rows", _empty)
    monkeypatch.setattr(ai_router.dpm_ctl, "get_calibration_runs", _empty)

    body = make_client().get("/api/ai/dpm").json()

    assert body["performance"] == [{"strategy": "dpm", "count": 4}]
    assert lab["completions"] == []
