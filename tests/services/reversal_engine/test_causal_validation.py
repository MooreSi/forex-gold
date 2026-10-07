"""Time causality uses deterministic spans; no broker or network."""
from backend.src.services.market import validation as val


def test_forward_folds_do_not_train_on_future_or_unresolved_labels():
    spans = [(i*100.0, i*100.0+10.0) for i in range(20)]
    spans[3] = (300.0, 5000.0)
    folds = val.purged_walk_forward(20, 3, spans, embargo=50.0)
    train, test = folds[0]
    assert test == list(range(5, 10))
    assert train == [0, 1, 2, 4]


def test_embargo_boundary_is_excluded_and_input_order_is_preserved():
    spans = [(0, 20), (100, 250), (200, 240), (300, 310), (400, 410), (500, 510)]
    train, test = val.purged_walk_forward(6, 1, spans, embargo=50)[0]
    assert train == [0, 2]
    assert test == [3, 4, 5]
