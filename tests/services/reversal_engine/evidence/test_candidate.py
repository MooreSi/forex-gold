"""Deterministic candidate evaluation; no model or broker writes."""
from backend.src.services.reversal_engine.evidence.candidate import evaluate


def rows(net=-1):
    return [{"decision_ts": i * 3600, "available_at": i * 3600 + 60,
             "features": [float(i)], "r": net, "champion_prediction": .1} for i in range(100)]


def test_negative_selected_payoff_cannot_be_eligible():
    result = evaluate(rows(), fit_predict=lambda train, test: [.2] * len(test))
    assert result["mean_selected_r"] == -1
    assert result["eligible"] is False


def test_training_never_uses_labels_not_yet_available():
    seen = []
    def predict(train, test):
        seen.append(max(r["available_at"] for r in train) < min(r["decision_ts"] for r in test))
        return [.2] * len(test)
    evaluate(rows(1), fit_predict=predict)
    assert seen == [True, True, True, True]


def test_small_sample_cannot_promote():
    assert evaluate(rows(1)[:5])["refusal"] == "fewer than 80 settled broker decisions"


def test_benchmark_counts_actual_executed_winners_even_without_a_champion_score():
    data = [{**r, "champion_prediction": None} for r in rows(1)]
    result = evaluate(data, fit_predict=lambda train, test: [-.2] * len(test))
    assert result["paired_delta_lower_r"] == -1
