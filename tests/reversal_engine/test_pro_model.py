"""Contract for the pro-likeness sub-model.

Expected behaviour, from the feature spec:

  * It refuses to speak until the corpus is honest: enough positives, enough
    negatives, and an RSI spread wide enough that the sample is not one
    single market regime. Until then every score is the neutral 0.5 -- the
    same value the Reversal Engine's feature carries when the model does not
    exist at all, because both mean "no information".
  * It refuses to speak when it cannot beat a coin out of sample.
  * When it IS live, a moment resembling the professionals' entries must
    score higher than one that does not.
  * A call that reached TP1 counts for more than one that was stopped out,
    but a stopped-out entry is still evidence of where they choose to act
    and is never discarded.
  * Its inputs are limited to what the live scoring path can supply, so a
    score computed from a corpus row and from a live signal with identical
    conditions must agree.
"""
import json
import os
import tempfile
import time

import pytest

from forex_trader.reversal_engine import pro_corpus, pro_model
from forex_trader.reversal_engine import reversal_engine_repo as re_repo


@pytest.fixture
def corpus():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_repo.init(path)
    pro_corpus.create_schema()
    pro_model._state.update(model=None, scaler=None, auc=None, n=0,
                            fitted_at=0.0, rows_at_fit=-1, reason="not fitted")
    yield pro_corpus
    os.remove(path)


def _row(msg_id, stage, rsi, adx, direction="BUY", fvg=1.0):
    return {
        "tg_message_id": msg_id, "stage": stage,
        "group_name": "Gold Diggers VIP", "direction": direction,
        "signal_ts": time.time(), "captured_at": time.time(),
        "capture_lag_s": 3.0,
        "entry_low": 4100.0, "entry_high": 4102.0,
        "stop_loss": 4090.0, "tp1": 4120.0,
        "bid": 4101.0, "ask": 4101.3, "spread_points": 30.0, "price": 4101.0,
        "dist_to_entry_mid": 0.0, "price_inside_zone": 1,
        "session": "london", "regime_score": 1.0,
        "indicators_json": json.dumps(
            {"M15": {"rsi14": rsi, "adx14": adx, "atr14": 6.0}}),
        "fvg_json": json.dumps({"fvg_confluence": fvg, "fvg_dist_norm": 0.5,
                                "fvg_fresh": 1.0, "fvg_size_norm": 0.9}),
        "raw_text": "",
    }


def insert_resolved(corpus, msg_id, outcome, **kw):
    """Capture a signal, then resolve it the way pro_outcome would -- outcomes
    are never written by the capture path."""
    corpus.insert(_row(msg_id, "complete", **kw))
    snap_id = [r["id"] for r in corpus.unresolved(min_age_s=0.0)
               if r["tg_message_id"] == msg_id][0]
    corpus.set_outcome(snap_id, outcome, 1.5 if outcome == "win" else -1.0)


def seed_separable(corpus, n=70):
    """A corpus the model can actually learn from: they fire on oversold
    pullbacks in a trend, background samples are overbought and directionless.
    The RSI range spans both, so the single-regime gate is satisfied.
    """
    for i in range(n):
        corpus.insert(_row(f"pro-{i}", "complete", rsi=32 + (i % 7), adx=30 + (i % 5)))
        corpus.insert(_row(f"bg-{i}", "background", rsi=72 + (i % 7), adx=14 + (i % 5),
                           fvg=0.0))


# ── Refusing to speak ─────────────────────────────────────────────────────────

def test_an_empty_corpus_scores_every_moment_neutral(corpus):
    score = pro_model.pro_likeness("BUY", 62.0, 30.0, 6.0, 1.0)

    assert score == pro_model.NEUTRAL


def test_too_few_pro_signals_keeps_the_model_silent(corpus):
    seed_separable(corpus, n=10)

    pro_model.fit(force=True)

    assert pro_model.status()["ready"] is False
    assert "pro signals" in pro_model.status()["reason"]
    assert pro_model.pro_likeness("BUY", 32.0, 30.0, 6.0, 1.0) == pro_model.NEUTRAL


def test_a_single_regime_sample_keeps_the_model_silent(corpus):
    # Plenty of rows, but every one of them recorded inside a narrow RSI band:
    # a model fitted here learns the week, not their logic.
    for i in range(70):
        corpus.insert(_row(f"pro-{i}", "complete", rsi=68 + (i % 3), adx=30))
        corpus.insert(_row(f"bg-{i}", "background", rsi=70 + (i % 3), adx=20))

    pro_model.fit(force=True)

    assert pro_model.status()["ready"] is False
    assert "single-regime" in pro_model.status()["reason"]


def test_a_model_that_cannot_beat_a_coin_is_not_used(corpus):
    # Positives and negatives drawn from the same distribution -- there is
    # nothing to learn, and the honest response is silence rather than noise.
    for i in range(70):
        corpus.insert(_row(f"pro-{i}", "complete", rsi=30 + (i % 45), adx=20 + (i % 20)))
        corpus.insert(_row(f"bg-{i}", "background", rsi=30 + (i % 45), adx=20 + (i % 20)))

    pro_model.fit(force=True)

    assert pro_model.status()["auc"] < pro_model.NEUTRAL + 0.15
    assert pro_model.status()["ready"] is False
    assert pro_model.pro_likeness("BUY", 40.0, 25.0, 6.0, 1.0) == pro_model.NEUTRAL


# ── Speaking ──────────────────────────────────────────────────────────────────

def test_a_learnable_corpus_produces_a_usable_model(corpus):
    seed_separable(corpus)

    pro_model.fit(force=True)

    status = pro_model.status()
    assert status["ready"] is True
    assert status["auc"] > pro_model._MIN_AUC


def test_a_pro_like_moment_scores_higher_than_an_unlike_one(corpus):
    seed_separable(corpus)
    pro_model.fit(force=True)

    like = pro_model.pro_likeness("BUY", 33.0, 32.0, 6.0, 1.0,
                                  {"fvg_confluence": 1.0, "fvg_dist_norm": 0.5,
                                   "fvg_fresh": 1.0, "fvg_size_norm": 0.9})
    unlike = pro_model.pro_likeness("BUY", 75.0, 14.0, 6.0, 1.0,
                                    {"fvg_confluence": 0.0, "fvg_dist_norm": 0.5,
                                     "fvg_fresh": 1.0, "fvg_size_norm": 0.9})

    assert like > unlike


def test_the_corpus_and_the_live_path_agree_on_the_same_conditions(corpus):
    # A feature present in training but missing in production would silently
    # degrade every live score. Same conditions, both routes, same vector.
    seed_separable(corpus)
    row = corpus.rows(background=False)[0]

    from_corpus = pro_model._row_vector(row)
    from_live = pro_model._vector(
        row["direction"], 32.0, 30.0, 6.0, row["regime_score"],
        {"fvg_confluence": 1.0, "fvg_dist_norm": 0.5,
         "fvg_fresh": 1.0, "fvg_size_norm": 0.9})

    assert from_corpus == from_live


# ── Outcome weighting ─────────────────────────────────────────────────────────

def test_a_winning_call_carries_more_weight_than_a_losing_one(corpus):
    insert_resolved(corpus, "won", "win", rsi=35, adx=30)
    insert_resolved(corpus, "lost", "loss", rsi=35, adx=30)

    _, _, weights, _ = pro_model._dataset()

    by_outcome = {r["outcome"]: w
                  for r, w in zip(corpus.rows(background=False), weights)}
    assert by_outcome["win"] > by_outcome["loss"]


def test_a_losing_call_is_still_learned_from(corpus):
    # Their judgement about WHERE to act is the target; a disciplined entry
    # that lost is weaker evidence of it, not absent evidence.
    insert_resolved(corpus, "lost", "loss", rsi=35, adx=30)

    _, labels, weights, _ = pro_model._dataset()

    assert labels == [1]
    assert weights[0] > 0


def test_an_unresolved_call_is_learned_from_at_full_weight(corpus):
    # The model must never stall waiting for outcomes to be walked forward.
    corpus.insert(_row("pending", "complete", rsi=35, adx=30))

    _, _, weights, _ = pro_model._dataset()

    assert weights == [1.0]


# ── Toggle behaviour ──────────────────────────────────────────────────────────

def test_the_engine_feature_stays_neutral_while_the_toggle_is_off(corpus, monkeypatch):
    from forex_trader.reversal_engine import ml_engine

    seed_separable(corpus)
    pro_model.fit(force=True)
    monkeypatch.setattr(ml_engine, "learning_from_ref_enabled", lambda: False)

    value = ml_engine._pro_likeness_feature(
        {"direction": "BUY", "rsi14": 33.0, "adx": 32.0, "atr": 6.0,
         "regime_score": 1.0, "session": "london", "fvg_confluence": 1.0})

    assert value == 0.5


def test_the_engine_feature_carries_the_model_once_the_toggle_is_on(corpus, monkeypatch):
    from forex_trader.reversal_engine import ml_engine

    seed_separable(corpus)
    pro_model.fit(force=True)
    monkeypatch.setattr(ml_engine, "learning_from_ref_enabled", lambda: True)

    value = ml_engine._pro_likeness_feature(
        {"direction": "BUY", "rsi14": 33.0, "adx": 32.0, "atr": 6.0,
         "regime_score": 1.0, "session": "london", "fvg_confluence": 1.0,
         "fvg_dist_norm": 0.5, "fvg_fresh": 1.0, "fvg_size_norm": 0.9})

    assert value != 0.5
    assert 0.0 <= value <= 1.0


def test_the_feature_vector_has_one_slot_per_declared_feature_name(corpus, monkeypatch):
    from forex_trader.reversal_engine import ml_engine

    monkeypatch.setattr(ml_engine, "learning_from_ref_enabled", lambda: False)

    feats = ml_engine.extract_features(
        {"direction": "BUY", "level_type": "round_10", "level_score": 0.7,
         "session": "london", "adx": 30.0, "atr": 6.0, "rr_tp1": 1.5})

    assert len(feats) == len(ml_engine.FEATURE_NAMES)
    assert feats[ml_engine.FEATURE_NAMES.index("pro_likeness")] == 0.5
