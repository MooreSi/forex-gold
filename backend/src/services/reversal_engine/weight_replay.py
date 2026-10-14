"""Walk-forward replay of the Reversal batch model under different training
weights. docs/todo/017 step 1b; the protocol is pre-registered in that spec.

Every `block` test rows, fit on everything whose label had closed before the
block's first signal, and score the block. Nothing a test row's prediction
uses was known after that row was created.

Pure: rows in, predictions and statistics out. The database read and the
printing are in `tools/ml_weight_replay.py`. Nothing here loads, saves or
replaces a live model, reads a setting, or touches an order.
"""
from __future__ import annotations

import bisect
import math
import random
from typing import Callable, Optional, Sequence

CLUSTER_S = 2 * 3600.0


def replay(rows: Sequence[dict], weight_fn: Callable[[list], Optional[list]],
           fit_fn: Callable, n_test: int, block: int,
           min_train: int = 50) -> list[tuple[dict, float]]:
    """[(row, prediction)] for the newest `n_test` rows, oldest first.

    `rows` need `created_at`, `close_time`, `x` and `y`, sorted by
    `created_at`. `weight_fn(train_rows)` returns weights, or None for the
    fit's own default. `fit_fn(X, y, w_or_None)` returns a model with
    `.predict`."""
    import numpy as np
    rows = list(rows)
    test = rows[-n_test:]
    by_close = sorted(rows[:len(rows)], key=lambda r: r["close_time"])
    closes = [r["close_time"] for r in by_close]
    out = []
    for i in range(0, len(test), block):
        chunk = test[i:i + block]
        cutoff = chunk[0]["created_at"]
        train = by_close[:bisect.bisect_left(closes, cutoff)]
        train.sort(key=lambda r: r["created_at"])
        if len(train) < min_train:
            continue
        w = weight_fn(train)
        model = fit_fn([r["x"] for r in train], [r["y"] for r in train], w)
        preds = model.predict(np.array([r["x"] for r in chunk], dtype=float))
        out.extend(zip(chunk, (float(p) for p in preds)))
    return out


def _ranks(v: Sequence[float]) -> list[float]:
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2.0 + 1.0
        i = j + 1
    return r


def spearman(a: Sequence[float], b: Sequence[float]) -> Optional[float]:
    if len(a) < 3:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = math.sqrt(sum((x - ma) ** 2 for x in ra))
    vb = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return cov / (va * vb) if va > 0 and vb > 0 else None


def lift(preds: Sequence[float], r: Sequence[float]) -> Optional[float]:
    """Mean R of rows the gate passes (prediction >= 0) minus mean R of all."""
    passed = [y for p, y in zip(preds, r) if p >= 0]
    if not passed or not r:
        return None
    return sum(passed) / len(passed) - sum(r) / len(r)


def block_bootstrap(ts: Sequence[float], stat: Callable[[list], Optional[float]],
                    n: int = 2000, seed: int = 17, level: float = 0.95) -> tuple:
    """(lo, hi) of `stat(indices)` over resamples of whole `CLUSTER_S`
    blocks. `stat` takes a list of row indices."""
    blocks: dict[int, list[int]] = {}
    for i, t in enumerate(ts):
        blocks.setdefault(int(float(t) // CLUSTER_S), []).append(i)
    keys = list(blocks)
    rng = random.Random(seed)
    vals = []
    for _ in range(n):
        idx = [i for k in (rng.choice(keys) for _ in keys) for i in blocks[k]]
        v = stat(idx)
        if v is not None and math.isfinite(v):
            vals.append(v)
    if not vals:
        return (None, None)
    vals.sort()
    a = (1.0 - level) / 2.0
    return vals[int(a * (len(vals) - 1))], vals[int((1.0 - a) * (len(vals) - 1))]
