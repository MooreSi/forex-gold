"""Training-sample weights by time, with a floor, discounted for overlap.

docs/todo/017 item 1. Both engines' batch models used to weight row i by its
POSITION (`exp(-0.017 * rows_from_newest)` for Reversal, a 40-trade half-life
for Breakout). The Reversal engine writes 100-150 rows a day, so on
2026-10-14 its 5,431 trainable rows had an effective sample size of 118 and 90%
of the weight sat on the newest 101 hours: the model saw roughly one regime
and could not learn how market conditions change an outcome.

The weights here follow Lopez de Prado (AFML ch. 4) in shape, not in every
detail: decay is by the label's AGE in days with a floor, so an old regime
keeps a fixed share of a say, and each row is multiplied by its average
uniqueness, so ten signals that rode one move count as roughly one. Age is
measured from the newest label in the set, not the wall clock, so the same
rows always give the same weights.

`uniqueness` is `validation.uniqueness_weights` computed with a sweep and
prefix sums: the reference is quadratic in the row count, and a refit sees
thousands of rows every fifth close. Pinned equal to it on random spans.

Pure. Nothing here reads a setting or touches an order.
"""
from __future__ import annotations

import bisect
from typing import Sequence

Span = tuple[float, float]

_DAY_S = 86400.0


def uniqueness(spans: Sequence[Span]) -> list[float]:
    """Average uniqueness per span, identical to
    `validation.uniqueness_weights` (see its docstring for the definition)."""
    n = len(spans)
    if n == 0:
        return []
    starts = sorted(float(a) for a, _ in spans)
    ends = sorted(float(b) for _, b in spans)
    edges = sorted({float(t) for s in spans for t in s})
    index = {t: i for i, t in enumerate(edges)}

    # Prefix sums over the elementary intervals between consecutive edges:
    # their length, and their length divided by how many spans are live.
    p_len = [0.0]
    p_inv = [0.0]
    for a, b in zip(edges, edges[1:]):
        mid = (a + b) / 2.0
        live = bisect.bisect_left(starts, mid) - bisect.bisect_left(ends, mid)
        p_len.append(p_len[-1] + (b - a))
        p_inv.append(p_inv[-1] + (b - a) / max(1, live))

    out = []
    for a, b in spans:
        a, b = float(a), float(b)
        if b <= a:
            live = bisect.bisect_right(starts, a) - bisect.bisect_left(ends, a)
            out.append(1.0 / max(1, live))
            continue
        i, j = index[a], index[b]
        covered = p_len[j] - p_len[i]
        out.append((p_inv[j] - p_inv[i]) / covered if covered > 0 else 1.0)
    return out


def decay(ages_s: Sequence[float], half_life_days: float, floor: float) -> list[float]:
    """max(floor, 0.5 ** (age / half-life)) for each age in seconds."""
    if not half_life_days or half_life_days <= 0:
        raise ValueError("half_life_days must be positive")
    if not 0.0 <= floor <= 1.0:
        raise ValueError("floor must be within [0, 1]")
    hl = float(half_life_days) * _DAY_S
    return [max(floor, 0.5 ** (max(0.0, float(a)) / hl)) for a in ages_s]


def training_weights(spans: Sequence[Span], half_life_days: float,
                     floor: float) -> list[float]:
    """Decay by the age of each label's close times its uniqueness,
    normalised to a mean of 1 (the scale today's weights are fitted at)."""
    if not spans:
        return []
    newest = max(float(b) for _, b in spans)
    d = decay([newest - float(b) for _, b in spans], half_life_days, floor)
    u = uniqueness(spans)
    w = [x * y for x, y in zip(d, u)]
    total = sum(w)
    if total <= 0:
        return [1.0] * len(w)
    return [x * len(w) / total for x in w]


def effective_size(weights: Sequence[float]) -> float:
    """Kish's effective sample size, (sum w)^2 / sum w^2."""
    s2 = sum(x * x for x in weights)
    return (sum(weights) ** 2 / s2) if s2 > 0 else 0.0
