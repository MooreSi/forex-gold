"""Label publication time must precede proof data; no broker or live model."""
from backend.src.services.reversal_engine import edge_model as em


def test_edge_fold_excludes_an_old_entry_whose_label_arrived_late():
    times = [i*10000.0 for i in range(2000)]
    available = list(times)
    available[1] = times[-1]+1
    train, test = em.walk_forward(times, available)[0]
    assert 0 in train
    assert 1 not in train
    assert max(available[i] for i in train) < times[test[0]] - em.EMBARGO_S
