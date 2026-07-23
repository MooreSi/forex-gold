"""rank_eligible_candidates() is the extracted, pure core of task #34's
change: previously ml_prob was computed only AFTER a signal had already
been created for whichever level cleared level_detector's own score
first -- the model had zero influence on WHICH of several qualifying
levels became the signal (only on whether a live-money fill went ahead
afterwards, via _try_live_execute's separate re-check). _run_cycle now
builds up to _ML_CANDIDATE_POOL gate-passing candidates, feature-extracts
and predicts each one, then calls this function to pick the winner."""
from forex_trader.reversal_engine.reversal_engine_service import rank_eligible_candidates


def _candidate(level_price: float, prob):
    """(level, direction, sig_data, feats, prob) tuple shaped like
    _run_cycle's `eligible` list -- only price and prob matter for these
    tests, the rest are placeholders."""
    return ({"price": level_price, "type": "swing_high", "score": 0.8}, "BUY", {}, [0.0], prob)


def test_no_trained_model_keeps_original_level_score_order():
    """Cold start: every prob is None -- must not reorder at all, so
    behaviour before ML had a vote is unchanged exactly."""
    eligible = [_candidate(4010.0, None), _candidate(4020.0, None), _candidate(4030.0, None)]
    ranked = rank_eligible_candidates(eligible)
    assert [c[0]["price"] for c in ranked] == [4010.0, 4020.0, 4030.0]


def test_higher_predicted_r_multiple_wins_even_if_not_first_by_level_score():
    """The actual behaviour change: a weaker-by-level-score-but-still-
    eligible candidate (it's in the pool at all, so it already passed the
    0.50 score gate and bias filter) can now win if the model likes it
    more than the level_detector-first candidate."""
    weak_but_ml_likes_it  = _candidate(4010.0, prob=0.3)   # first by level score
    strong_by_ml          = _candidate(4020.0, prob=1.8)   # second by level score
    eligible = [weak_but_ml_likes_it, strong_by_ml]
    ranked = rank_eligible_candidates(eligible)
    assert ranked[0][0]["price"] == 4020.0


def test_candidates_missing_a_prediction_sort_last_not_crash():
    """extract_features()/predict() can both return None for an individual
    candidate (bad feature data) while others in the same pool have a real
    prediction -- must not raise, and the None one should rank behind any
    real (even negative) prediction."""
    no_prediction   = _candidate(4010.0, prob=None)
    predicted_loss  = _candidate(4020.0, prob=-0.5)
    eligible = [no_prediction, predicted_loss]
    ranked = rank_eligible_candidates(eligible)
    assert ranked[0][0]["price"] == 4020.0
    assert ranked[1][0]["price"] == 4010.0


def test_single_candidate_unchanged():
    eligible = [_candidate(4010.0, prob=0.42)]
    assert rank_eligible_candidates(eligible) == eligible


def test_empty_list_returns_empty():
    assert rank_eligible_candidates([]) == []
