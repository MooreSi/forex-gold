"""Broker-only challenger. All folds are causal; no function promotes a model."""
from __future__ import annotations
import math
import statistics
from collections import defaultdict
from backend.src.services.market.validation import purged_walk_forward

MIN_ROWS = 80
MODEL_PARAMS = {"max_iter": 100, "max_leaf_nodes": 7, "min_samples_leaf": 10,
                "learning_rate": 0.05, "l2_regularization": 1.0, "random_state": 42}


def fit(rows):
    from sklearn.ensemble import HistGradientBoostingRegressor
    model = HistGradientBoostingRegressor(**MODEL_PARAMS)
    model.fit([r["features"] for r in rows], [r["r"] for r in rows])
    return model


def _predict(train, test):
    return fit(train).predict([r["features"] for r in test]).tolist()


def _cluster_lower(rows):
    days = defaultdict(list)
    for row in rows:
        days[int(row["decision_ts"] // 86400)].append(row["payoff"])
    means = [statistics.mean(v) for v in days.values()]
    lower = statistics.mean(means) - 1.96 * statistics.stdev(means) / math.sqrt(len(means)) if len(means) > 1 else None
    return lower, len(means)


def evaluate(rows, fit_predict=None):
    rows = sorted(rows, key=lambda r: r["decision_ts"])
    out = {"n": len(rows), "eligible": False, "refusal": "fewer than 80 settled broker decisions"}
    if len(rows) < MIN_ROWS:
        return out
    spans = [(r["decision_ts"], r["available_at"]) for r in rows]
    tested, selected, deltas, errors = [], [], [], []
    for train_idx, test_idx in purged_walk_forward(len(rows), 4, spans, embargo=60):
        if len(train_idx) < 10:
            continue
        train, test = [rows[i] for i in train_idx], [rows[i] for i in test_idx]
        predictions = (fit_predict or _predict)(train, test)
        if len(predictions) != len(test) or not all(math.isfinite(float(v)) for v in predictions):
            raise ValueError("candidate produced invalid predictions")
        for row, prediction in zip(test, predictions):
            take = prediction > 0  # fixed before evaluation; no threshold search
            # Every row is an executed broker trade: the deployed policy took it.
            # A missing score is not an avoided trade, and its threshold is not ours.
            champion_take = True
            tested.append(row)
            errors.append((prediction - row["r"]) ** 2)
            if take:
                selected.append({**row, "payoff": row["r"]})
            deltas.append({**row, "payoff": (row["r"] if take else 0) - (row["r"] if champion_take else 0)})
    if not tested:
        return {**out, "refusal": "no causal folds with known training labels"}
    lower, clusters = _cluster_lower(selected)
    delta_lower, _ = _cluster_lower(deltas)
    half = len(selected) // 2
    first = statistics.mean(r["r"] for r in selected[:half]) if half else None
    second = statistics.mean(r["r"] for r in selected[half:]) if selected[half:] else None
    out.update(n_oos=len(tested), n_selected=len(selected), clusters=clusters,
               mean_selected_r=statistics.mean(r["r"] for r in selected) if selected else None,
               mean_all_r=statistics.mean(r["r"] for r in tested), mse=statistics.mean(errors),
               first_half_r=first, second_half_r=second, clustered_lower_r=lower,
               paired_delta_lower_r=delta_lower)
    eligible = (all(r.get("costs_complete", False) for r in tested) and len(selected) >= 200 and clusters >= 30 and lower is not None and lower > 0
                and first is not None and first > 0 and second > 0
                and delta_lower is not None and delta_lower > 0)
    out.update(eligible=eligible, refusal="" if eligible else "insufficient positive, stable, incremental broker evidence")
    return out
