"""Trend PA's model: when it is allowed an opinion, and what the opinion does.

Measured on 2026-09-29 over 533 replayed trades (Nov 2023 - Sep 2026): a
logistic model on the setup features scored AUC 0.49-0.53 out of sample, so
the rule that matters most here is the ARMING rule. An unarmed model scores
nothing, and the panel says why, instead of filtering on noise.

Synthetic data only; nothing here reaches a broker.
"""
from __future__ import annotations

import random

import pytest

from backend.src.services.trend_pa import ml


def _rows(n, seed=1, signal=True, flip_after=None):
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        f = {k: rnd.uniform(-1, 1) for k in ml.FEATURES}
        x = f["wick_frac"]
        if flip_after is not None and i >= flip_after:
            x = -x
        win = (x + rnd.gauss(0, 0.3) > 0) if signal else (rnd.random() < 0.4)
        out.append({"features": f, "outcome": "win" if win else "loss",
                    "closed_at": 1000 + i})
    return out


def test_too_few_rows_is_not_armed():
    m = ml.Model()
    state = m.fit(_rows(ml.MIN_SAMPLES - 1))
    assert state["armed"] is False and "rows" in state["why"]
    assert m.predict(_rows(1)[0]["features"]) is None


def test_a_real_relationship_arms_it_and_it_ranks_the_right_way():
    m = ml.Model()
    state = m.fit(_rows(400))
    assert state["armed"] is True and state["auc"] > 0.8
    good = {k: 0.0 for k in ml.FEATURES} | {"wick_frac": 0.9}
    bad = {k: 0.0 for k in ml.FEATURES} | {"wick_frac": -0.9}
    assert m.predict(good) > m.predict(bad)


def test_noise_does_not_arm_it():
    m = ml.Model()
    state = m.fit(_rows(400, seed=7, signal=False))
    assert state["armed"] is False
    assert m.predict(_rows(1)[0]["features"]) is None


def test_the_holdout_is_the_most_recent_trades():
    """A relationship that held early and reversed late must fail the
    holdout. A random split would mix the two and could pass."""
    m = ml.Model()
    state = m.fit(_rows(400, flip_after=300))
    assert state["armed"] is False and state["auc"] < 0.5


def test_rows_are_ordered_by_close_time_before_splitting():
    rows = _rows(400, flip_after=300)
    m = ml.Model()
    assert m.fit(list(reversed(rows)))["armed"] is False


def test_missing_features_are_neutral_not_an_error():
    m = ml.Model()
    m.fit(_rows(400))
    assert m.predict({"wick_frac": 0.5}) is not None


def test_a_model_round_trips_through_a_file(tmp_path):
    m = ml.Model()
    m.fit(_rows(400))
    path = tmp_path / "m.pkl"
    m.save(path)
    again = ml.Model.load(path)
    f = _rows(1, seed=3)[0]["features"]
    assert again.predict(f) == pytest.approx(m.predict(f))
    assert again.state["armed"] is True


def test_a_missing_or_broken_file_loads_as_unarmed(tmp_path):
    assert ml.Model.load(tmp_path / "absent.pkl").state["armed"] is False
    bad = tmp_path / "bad.pkl"
    bad.write_bytes(b"not a pickle")
    assert ml.Model.load(bad).state["armed"] is False


@pytest.mark.parametrize("p,expected", [
    (None, True),     # unarmed: no opinion, the gate does not refuse
    (0.30, False),
    (0.34, False),    # 1:2 with 0.15R of cost breaks even at 0.3833
    (0.40, True),
])
def test_the_gate_asks_for_positive_expected_r(p, expected):
    assert ml.gate_passes(p, rr=2.0, cost_r=0.15) is expected
