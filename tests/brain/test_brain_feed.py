"""The brain feed: every recorded entry decision as an event, and which gate
decided it (docs/todo/008).

Read-only by construction. It reads three logs the app already writes -- the
Telegram decision log (`tg_decisions`), the Reversal engine's
`re_signals.live_exec_status` and the Breakout engine's
`bo_signals.live_exec_status` -- and the gates' current state. It never writes
anything and never reaches a broker.

The classification matters because the picture is only worth looking at if it
is true: a signal the schedule held must stop at the schedule, not somewhere
plausible. Each rule below is pinned against a reason string the app has
really produced (taken from the demo install on 2026-09-27).
"""
from __future__ import annotations

import pytest

from backend.src.services.brain import feed
from backend.src.services.brain import gates


# ── Classification ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason, executed, outcome, gate", [
    ("", True, "executed", "broker"),
    (None, True, "executed", "broker"),
    ("Auto-execution is OFF — activate manually in the dashboard.", True, "blocked", "auto"),
    ("skipped:live_off", False, "blocked", "auto"),
    ("error:Trading stood down — the VPS is the active trader (see", False, "blocked", "node"),
    ("Auto-execution failed: Trading paused until 22:00 — MT5 order blocked:", False, "blocked", "halt"),
    ("Trade blocked — circuit breaker active. Live trading resumes in approx", False, "blocked", "breaker"),
    ("GOLD X daily loss cap reached ($-120.00 today, cap $100.00)", False, "blocked", "loss_cap"),
    ("Auto-execution skipped — Trading Schedule: daily profit target reached", False, "blocked", "schedule"),
    ("Gold Diggers Scalping disabled for this window (Trading > Schedule)", False, "blocked", "schedule"),
    ("skipped:schedule", False, "blocked", "schedule"),
    ("News blackout: NFP in 4 min (Trading > News)", False, "blocked", "news"),
    ("ml_skipped", False, "blocked", "model"),
    ("momentum_skipped", False, "blocked", "model"),
    ("bias_skipped", False, "blocked", "model"),
    ("skipped:unproven_edge", False, "blocked", "model"),
    ("Auto-execution skipped — BUY zone $4273.00–$4277.00 already breached (", False, "blocked", "entry"),
    ("stale instant message", False, "blocked", "entry"),
    ("Signal queued — BUY price $4294.43 is above the entry zone $4288.00–$4", False, "queued", "entry"),
    ("max open trades (3) reached", False, "blocked", "slots"),
    ("error:Max open trades reached (3) — 3 open, 0 resting at the", False, "blocked", "slots"),
    ("Auto-execution failed: EA rejected template order: invalid volume", False, "blocked", "broker"),
    ("skipped:exposure_guard", False, "blocked", "risk"),
    ("error:R:R filter blocked: TP1 is 3.0 pts from zone mid vs SL 7.0 pts away", False, "blocked", "risk"),
    ("error:Sig Guard: a template-managed trade is already open for 'Reversal Engine' BUY", False, "blocked", "risk"),
    ("filled_too_soon", False, "blocked", "entry"),
    ("error:Auto: Reversal Engine in a ranging market -> Auto Limit Balanced (Trading > Schedule: Auto)", False, "blocked", "model"),
    ("no live price", False, "blocked", "broker"),
    ("error:EA Template strategies require a connected, healthy EA — none is available right now", False, "blocked", "broker"),
    ("error:Trading paused until 22:00 — MT5 order blocked: Daily loss limit hit: $-178.45", False, "blocked", "halt"),
    ("something nobody has seen before", False, "blocked", "other"),
])
def test_each_real_reason_stops_at_its_gate(reason, executed, outcome, gate):
    assert gates.classify(reason, executed) == (outcome, gate)


def test_every_gate_a_reason_can_reach_is_on_the_map():
    keys = {g["key"] for g in gates.GATES}
    for rule_gate in gates.rule_gates():
        assert rule_gate in keys
    assert "broker" in keys and "other" in keys


# ── Events ──────────────────────────────────────────────────────────────────

@pytest.fixture
def logs(monkeypatch):
    rows = {
        "tg": [{"id": 3, "ts": 300.0, "source": "GOLD X", "direction": "BUY",
                "executed": 1, "reason": ""},
               {"id": 2, "ts": 100.0, "source": "GOLD Y", "direction": "SELL",
                "executed": 0, "reason": "max open trades (3) reached"}],
        "re": [{"id": 9, "ts": 200.0, "direction": "SELL", "status": "ml_skipped"}],
        "bo": [{"id": 4, "ts": 250.0, "direction": "BUY", "status": "success"}],
    }
    monkeypatch.setattr(feed.feed_repo, "recent_telegram", lambda limit: rows["tg"])
    monkeypatch.setattr(feed.feed_repo, "recent_reversal", lambda limit: rows["re"])
    monkeypatch.setattr(feed.feed_repo, "recent_breakout", lambda limit: rows["bo"])
    return rows


def test_events_merge_every_source_newest_first(logs):
    events = feed.events(limit=10)
    assert [e["key"] for e in events] == ["tg:3", "bo:4", "re:9", "tg:2"]


def test_an_event_carries_what_the_picture_needs(logs):
    e = {x["key"]: x for x in feed.events(limit=10)}
    assert e["tg:2"] == {
        "key": "tg:2", "ts": 100.0, "kind": "telegram", "source": "GOLD Y",
        "direction": "SELL", "outcome": "blocked", "gate": "slots",
        "reason": "max open trades (3) reached",
    }
    assert e["re:9"]["source"] == "Reversal Engine"
    assert e["re:9"]["reason"] == "the ML model scored it below its floor"
    assert e["re:9"]["gate"] == "model"
    assert e["bo:4"]["outcome"] == "executed"


def test_the_engines_success_words_both_mean_executed(logs):
    """They do not share a word (engines README, bugs/062): the Reversal
    engine writes 'executed', the Breakout engine 'success'."""
    logs["re"][0]["status"] = "executed"
    assert {e["key"]: e["outcome"] for e in feed.events(10)}["re:9"] == "executed"
    assert {e["key"]: e["outcome"] for e in feed.events(10)}["bo:4"] == "executed"


def test_the_limit_is_applied_after_merging(logs):
    assert [e["key"] for e in feed.events(limit=2)] == ["tg:3", "bo:4"]


def test_one_unreadable_log_does_not_blank_the_others(logs, monkeypatch):
    def _boom(limit):
        raise RuntimeError("reversal db not open")

    monkeypatch.setattr(feed.feed_repo, "recent_reversal", _boom)
    monkeypatch.setattr(feed.feed_repo, "recent_telegram", _boom)
    assert [e["key"] for e in feed.events(10)] == ["bo:4"]


# ── Gate states ─────────────────────────────────────────────────────────────

def test_a_gate_state_that_cannot_be_read_is_unknown_not_open(monkeypatch):
    """None, never False: an unreadable breaker is not a breaker that is off."""
    def _boom():
        raise RuntimeError("no settings table")

    monkeypatch.setattr(feed, "_breaker", _boom)
    states = {g["key"]: g for g in feed.gate_states()}
    assert states["breaker"]["blocking"] is None


def test_gate_states_cover_every_gate(monkeypatch):
    for name in ("_auto", "_halt", "_breaker", "_schedule", "_loss_cap", "_news", "_slots"):
        monkeypatch.setattr(feed, name, lambda: (False, ""))
    states = feed.gate_states()
    assert [s["key"] for s in states] == [g["key"] for g in gates.GATES]
    assert {s["key"]: s["blocking"] for s in states}["halt"] is False


def test_snapshot_has_the_gates_and_the_events(logs, monkeypatch):
    monkeypatch.setattr(feed, "gate_states", lambda: [{"key": "halt"}])
    snap = feed.snapshot()
    assert snap["gates"] == [{"key": "halt"}]
    assert len(snap["events"]) == 4
    assert snap["now"] > 0
