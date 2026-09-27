"""The Breakout batch review hands its adjustments to the tuning ledger, and
every close gives the ledger a chance to judge (docs/todo/007).

Without these two wires the ledger would be a module nothing calls: record
mode would record nothing, and approve mode would never stop the batch from
applying changes.

No test reaches a broker, a database or an AI provider: the provider returns
a canned reply and the repo, the ML summary and the ledger are recorders.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.breakout_signal import breakout_signal_learn as learn
from backend.src.services.breakout_signal import breakout_signal_manage as mg
from backend.src.services.breakout_signal import tuning_ledger


class _Repo:
    def __init__(self):
        self.logged = []

    def get_recent_closed_signals(self, limit):
        return [{"id": i, "direction": "BUY", "breakout_type": "go", "outcome": "loss",
                 "pnl_pts": -3.0, "net_pnl_pts": -3.4, "net_pnl_dollars": -34.0,
                 "session": "london", "htf_bias": "bull", "adx_at_signal": 30.0,
                 "rr_tp1": 1.5, "ml_prob": None} for i in range(4)]

    def get_stats(self):
        return {"wins": 1, "losses": 3, "be": 0, "win_rate": 25.0, "avg_pnl_dollars": -20.0}

    def get_perf_by_session(self): return []
    def get_perf_by_breakout_type(self): return []
    def get_perf_by_adx_band(self): return []
    def get_perf_by_bias(self): return []
    def get_virtual_balance(self): return 900.0

    def log_analysis(self, row):
        self.logged.append(row)


class _Engine(learn._LearnMixin):
    pass


@pytest.fixture
def batch(monkeypatch):
    repo = _Repo()
    handed = []
    reply = {"adjustments": [{"param": "min_adx_go", "new_value": 32, "reason": "r"}],
             "summary": "tighten"}

    async def _complete(*a, **k):
        return json.dumps(reply)

    monkeypatch.setattr(learn, "bdb", repo)
    monkeypatch.setattr(learn.ap, "catalogue_for_prompt", lambda: "min_adx_go: 28")
    monkeypatch.setattr(learn.cfg_module, "load", lambda: {})
    monkeypatch.setattr(learn.ai_provider, "is_configured", lambda cfg: True)
    monkeypatch.setattr(learn.ai_provider, "complete", _complete)
    monkeypatch.setattr(
        "backend.src.services.breakout_signal.ml_engine.summary",
        lambda: {"trained": False, "labeled_count": 0, "min_needed": 30,
                 "has_batch": False, "has_online": False})

    def _handle(adjustments, summary=""):
        handed.append((adjustments, summary))
        return ["min_adx_go→32"]

    monkeypatch.setattr(tuning_ledger, "handle_batch", _handle)
    return {"repo": repo, "handed": handed}


def test_the_batch_hands_every_adjustment_to_the_ledger(batch):
    asyncio.run(_Engine()._run_batch_analysis())
    assert batch["handed"] == [(
        [{"param": "min_adx_go", "new_value": 32, "reason": "r"}], "tighten")]


def test_the_batch_log_still_names_what_was_applied(batch):
    asyncio.run(_Engine()._run_batch_analysis())
    assert "min_adx_go→32" in batch["repo"].logged[-1]["claude_decision"]


def test_the_batch_never_applies_a_param_itself(batch, monkeypatch):
    """The ledger is the only route: approve mode depends on it."""
    def _boom(*a, **k):
        raise AssertionError("batch applied a param directly")

    monkeypatch.setattr(learn.ap, "apply_adjustment", _boom)
    asyncio.run(_Engine()._run_batch_analysis())
    assert batch["handed"]


# ── The close hook ──────────────────────────────────────────────────────────

class _CloseRepo:
    def get_signal_by_id(self, sig_id):
        return {"id": 1, "signal_ref": "BO-1", "strategy": "conservative",
                "created_at": 0.0, "remaining_frac": 1.0,
                "partial_pnl_dollars": 0.0, "mt5_ticket": None}

    def close_signal(self, *a, **k): pass


class _ManageEngine(mg._ManagementMixin):
    def __init__(self):
        self._closed_count = 0

    async def _run_batch_analysis(self): pass


@pytest.fixture
def close_lab(monkeypatch):
    calls = []
    monkeypatch.setattr(mg, "bdb", _CloseRepo())
    monkeypatch.setattr("backend.src.services.breakout_signal.ml_engine.record_outcome",
                        lambda *a: None)
    monkeypatch.setattr("backend.src.services.cluster.sync.ledger.push_trade_closed",
                        lambda payload: None)
    monkeypatch.setattr("backend.src.db.database.close_bus_entry", lambda *a: None)
    return calls


def test_every_close_asks_the_ledger_to_judge(close_lab, monkeypatch):
    monkeypatch.setattr(tuning_ledger, "evaluate", lambda: close_lab.append("evaluate"))
    _ManageEngine()._close_and_learn(1, 4012.0, "win", "n", 4002.0, "BUY", 0.1, 0.6)
    assert close_lab == ["evaluate"]


def test_a_failing_ledger_never_breaks_a_close(close_lab, monkeypatch):
    def _boom():
        raise RuntimeError("ledger table gone")

    monkeypatch.setattr(tuning_ledger, "evaluate", _boom)
    engine = _ManageEngine()
    engine._close_and_learn(1, 4012.0, "win", "n", 4002.0, "BUY", 0.1, 0.6)
    assert engine._closed_count == 1
