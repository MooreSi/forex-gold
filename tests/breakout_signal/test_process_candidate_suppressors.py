"""What stands between a passed candidate and a signal row.

`_process_candidate` was the largest untested block in `breakout_signal`:
132 statements never executed. It is the step immediately after the gates
pinned in `test_velocity_break_guards.py` -- those decide whether a
candidate exists, this decides whether it becomes a signal, and a signal is
what `_execute_live` turns into an MT5 order.

Seven suppressors sit in front of `bdb.create_signal`, and none of them was
pinned. Every refusal test here asserts `repo.created == []` rather than
"it returned early": a suppressor that stops logging but still creates the
signal would satisfy the weaker claim.

**No test reaches a broker, a database or Claude.** The repo, the reviewer,
the ML engine, the risk calculator and the cross-engine bus are all
stand-ins, and `_maybe_stage_grid_template` is replaced by a recorder, so
nothing downstream of signal creation can dispatch anything.

One behaviour here is characterised rather than endorsed: a
velocity-triggered suppression writes **no** analysis row (`if not
velocity` guards every `log_analysis` call on the refusal paths), so those
suppressions are invisible in the analysis log. That is what the code does
on purpose; `TestTheVelocityLogAsymmetry` states it so a change to it is
deliberate rather than accidental.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.breakout_signal import breakout_signal_service as svc

_NOW = 1_758_000_000.0


class _Repo:
    """Every repo call this function makes, recording instead of writing."""

    def __init__(self):
        self.created = []
        self.analysis = []
        self.ml_features = []
        self.ml_probs = []
        self.last_level_time = None
        self.recent_outcomes = []
        self.open_signals = []

    def log_analysis(self, entry):
        self.analysis.append(dict(entry))

    def get_last_signal_time_for_level(self, direction, level, cutoff):
        return self.last_level_time

    def get_recent_outcomes_by_direction(self, direction, cutoff, limit):
        return list(self.recent_outcomes)

    def get_open_signals(self):
        return list(self.open_signals)

    def create_signal(self, data):
        self.created.append(dict(data))
        return 9001

    def store_ml_features(self, sig_id, feats):
        self.ml_features.append((sig_id, feats))

    def store_ml_prob(self, sig_id, prob):
        self.ml_probs.append((sig_id, prob))


class _Engine(svc.BreakoutEngine):
    """The real method, with everything downstream of it recorded.

    Built without __init__: the engine's constructor opens loggers and
    config, none of which this function touches.
    """

    def __init__(self):
        self.status_detail = ""
        self.staged = []
        self.refreshes = 0

    def _notify_refresh(self):
        self.refreshes += 1

    async def _maybe_stage_grid_template(self, sig_id, price, tick):
        self.staged.append((sig_id, price, tick))
        return False


_CANDIDATE = {
    "direction": "BUY",
    "breakout_type": "break_and_go",
    "broken_level": 3998.0,
    "broken_level_type": "swing_high",
}

_CONTEXT = {
    "price": 4002.0, "session": "london", "htf_bias": "bullish",
    "h4_bias": "bullish", "macd_hist": 0.42,
}

_RISK = {
    "entry_mid": 4002.0, "stop_loss": 3990.0,
    "tp1": 4010.0, "tp2": 4018.0, "tp3": 4030.0,
}


@pytest.fixture
def lab(monkeypatch):
    """Every suppressor open. A test closes one."""
    repo = _Repo()
    state = {
        "repo": repo,
        "level_filter": 1.0,
        "blocked_levels": ("equal_highs",),
        "conflict": False,
        "risk": dict(_RISK),
        "review": {"approved": True, "score": 0.82,
                   "rationale": "clean break above the level", "fallback": False},
        "claude_enabled": 1,
        "labeled_count": 500,
        "ml_features": {"f": 1.0},
        "ml_pred": 0.35,
        "strategy": "conservative",
    }

    async def _review(candidate, risk, context):
        return dict(state["review"])

    monkeypatch.setattr(svc, "bdb", repo)
    monkeypatch.setattr(svc.ap, "get", lambda key: state["level_filter"])
    monkeypatch.setattr(svc, "review_signal", _review)
    monkeypatch.setattr(svc, "calculate_breakout_risk_levels",
                        lambda c, p, atr, adx: (dict(state["risk"])
                                                if state["risk"] else None))
    monkeypatch.setattr(svc.time, "time", lambda: _NOW)
    monkeypatch.setattr("backend.src.utils.regime.BREAKOUT_BLOCKED_LEVELS",
                        state["blocked_levels"])
    monkeypatch.setattr("backend.src.db.database.get_risk_settings",
                        lambda: {"bo_claude_eval_enabled": state["claude_enabled"],
                                 "trade_strategy": state["strategy"]})
    monkeypatch.setattr("backend.src.db.database.has_conflict_on_bus",
                        lambda *a, **kw: state["conflict"])
    monkeypatch.setattr("backend.src.db.database.write_signal_bus",
                        lambda *a, **kw: None)
    monkeypatch.setattr("backend.src.db.database.get_regime_score",
                        lambda adx, atr: 0.5)
    monkeypatch.setattr("backend.src.db.database.get_equity_drawdown_pct", lambda: 0.0)
    monkeypatch.setattr("backend.src.db.database.get_concurrent_agreement",
                        lambda *a: 0.0)
    monkeypatch.setattr(svc.bo_ml, "summary",
                        lambda: {"labeled_count": state["labeled_count"]})
    monkeypatch.setattr(svc.bo_ml, "extract_features",
                        lambda sig, market_ctx=None: state["ml_features"])
    monkeypatch.setattr(svc.bo_ml, "predict", lambda feats: state["ml_pred"])
    return state


def _run(state, velocity=False, candidate=None):
    engine = _Engine()
    log_entry = {"ts": _NOW}
    asyncio.run(engine._process_candidate(
        dict(candidate or _CANDIDATE), dict(_CONTEXT), log_entry,
        atr=9.5, adx=27.0, velocity=velocity,
    ))
    return engine, state["repo"]


def _result(repo):
    assert repo.analysis, "nothing was written to the analysis log"
    return repo.analysis[-1]["result"]


# ── Guard rails ──────────────────────────────────────────────────────────────

def test_a_clean_candidate_creates_exactly_one_signal(lab):
    engine, repo = _run(lab)

    assert len(repo.created) == 1
    assert _result(repo).startswith("signal_created:BO-")


def test_the_created_signal_carries_the_risk_levels_not_defaults(lab):
    _, repo = _run(lab)

    sig = repo.created[0]
    assert sig["stop_loss"] == 3990.0
    assert (sig["tp1"], sig["tp2"], sig["tp3"]) == (4010.0, 4018.0, 4030.0)
    assert sig["direction"] == "BUY"
    assert sig["broken_level"] == 3998.0
    assert sig["quality_score"] == 0.82
    assert sig["lot_size"] == svc._LOT_SIZE


def test_grid_legs_are_staged_for_a_created_signal(lab):
    engine, repo = _run(lab)

    assert engine.staged == [(9001, 4002.0, None)]


def test_the_ml_prediction_is_stored_against_the_new_signal(lab):
    _, repo = _run(lab)

    assert repo.ml_features == [(9001, {"f": 1.0})]
    assert repo.ml_probs == [(9001, 0.35)]


# ── The seven suppressors ────────────────────────────────────────────────────

def test_a_blocked_level_type_creates_nothing(lab):
    _, repo = _run(lab, candidate=dict(_CANDIDATE, broken_level_type="equal_highs"))

    assert repo.created == []
    assert _result(repo) == "level_type_blocked"


def test_the_level_filter_can_be_switched_off(lab):
    """Below 0.5 the blocked-level list is not consulted at all."""
    lab["level_filter"] = 0.0

    _, repo = _run(lab, candidate=dict(_CANDIDATE, broken_level_type="equal_highs"))

    assert len(repo.created) == 1


def test_a_level_that_fired_recently_is_on_cooldown(lab):
    lab["repo"].last_level_time = _NOW - 600

    _, repo = _run(lab)

    assert repo.created == []
    assert _result(repo) == "level_cooldown"


def test_three_consecutive_losses_stand_that_direction_down(lab):
    lab["repo"].recent_outcomes = ["loss", "loss", "loss"]

    _, repo = _run(lab)

    assert repo.created == []
    assert _result(repo) == "consec_loss_cooldown"


def test_a_win_among_the_recent_losses_does_not_stand_it_down(lab):
    """The rule is consecutive losses, not three losses somewhere."""
    lab["repo"].recent_outcomes = ["loss", "win", "loss"]

    _, repo = _run(lab)

    assert len(repo.created) == 1


def test_another_engine_holding_the_opposite_side_blocks_it(lab):
    lab["conflict"] = True

    _, repo = _run(lab)

    assert repo.created == []
    assert _result(repo) == "cross_engine_conflict"


def test_a_signal_with_no_workable_risk_levels_is_dropped(lab):
    lab["risk"] = None

    _, repo = _run(lab)

    assert repo.created == []
    assert _result(repo) == "risk_calc_failed"


def test_an_open_signal_in_the_same_direction_blocks_a_second(lab):
    lab["repo"].open_signals = [{"direction": "BUY"}]

    _, repo = _run(lab)

    assert repo.created == []
    assert _result(repo) == "duplicate_direction"


def test_an_open_signal_the_other_way_does_not_block(lab):
    lab["repo"].open_signals = [{"direction": "SELL"}]

    _, repo = _run(lab)

    assert len(repo.created) == 1


def test_a_claude_rejection_creates_nothing(lab):
    lab["review"] = {"approved": False, "score": 0.2,
                     "rationale": "into H4 resistance", "fallback": False}

    _, repo = _run(lab)

    assert repo.created == []
    assert _result(repo) == "claude_rejected"


# ── The bootstrap override ───────────────────────────────────────────────────
# A rejected signal still trades while the model has too little data to be
# worth obeying. This is the one place a refusal is overturned, so it is
# stated on its own.

class TestTheBootstrapOverride:

    def test_a_rejection_is_overturned_while_the_model_is_untrained(self, lab):
        lab["labeled_count"] = svc._BOOTSTRAP_SAMPLES - 1
        lab["review"] = {"approved": False, "score": 0.2,
                         "rationale": "into H4 resistance", "fallback": False}

        _, repo = _run(lab)

        assert len(repo.created) == 1
        assert repo.created[0]["rationale"].startswith("[bootstrap]")

    def test_it_stops_the_moment_there_is_enough_data(self, lab):
        lab["labeled_count"] = svc._BOOTSTRAP_SAMPLES
        lab["review"] = {"approved": False, "score": 0.2,
                         "rationale": "into H4 resistance", "fallback": False}

        _, repo = _run(lab)

        assert repo.created == []

    def test_a_claude_ERROR_is_never_overridden(self, lab):
        """fallback=True means the reviewer failed, not that it disapproved.
        Trading through a failed reviewer on the bootstrap excuse would turn
        an outage into unreviewed orders."""
        lab["labeled_count"] = 0
        lab["review"] = {"approved": False, "score": 0.0,
                         "rationale": "timeout", "fallback": True}

        _, repo = _run(lab)

        assert repo.created == []
        assert "Claude error" in repo.analysis[-1]["suppressed_reason"]


# ── The reviewer switch ──────────────────────────────────────────────────────

def test_with_claude_eval_off_the_reviewer_is_not_called(lab, monkeypatch):
    called = []

    async def _boom(*a, **kw):
        called.append(1)
        raise AssertionError("the reviewer was called with eval off")

    monkeypatch.setattr(svc, "review_signal", _boom)
    lab["claude_enabled"] = 0

    _, repo = _run(lab)

    assert called == []
    assert len(repo.created) == 1
    assert repo.created[0]["quality_score"] == 0.70


# ── The velocity asymmetry, characterised ────────────────────────────────────

class TestTheVelocityLogAsymmetry:
    """Velocity-triggered suppressions write no analysis row.

    Every `log_analysis` call on a refusal path is behind `if not
    velocity`, so a velocity candidate that is suppressed leaves no trace
    in the analysis log. Stated here so that changing it is a decision
    rather than an accident -- not endorsed: it means the velocity path's
    refusals cannot be counted afterwards.
    """

    def test_a_velocity_suppression_leaves_no_analysis_row(self, lab):
        lab["conflict"] = True

        _, repo = _run(lab, velocity=True)

        assert repo.created == []
        assert repo.analysis == []

    def test_the_same_suppression_on_the_candle_path_is_logged(self, lab):
        lab["conflict"] = True

        _, repo = _run(lab, velocity=False)

        assert repo.analysis[-1]["result"] == "cross_engine_conflict"

    def test_a_velocity_signal_that_is_created_IS_logged(self, lab):
        """Only the refusals are silent; a created signal is recorded
        either way."""
        _, repo = _run(lab, velocity=True)

        assert len(repo.created) == 1
        assert _result(repo).startswith("signal_created:")
