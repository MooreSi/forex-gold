"""Honest validation for a model whose labels overlap in time.

Section 5.2 of `docs/todo/reversal-engine/200`.

The reversal engine's ML has none of this. Its labels are path dependent and
overlapping -- a two-hour pending window, up to six concurrent open signals,
shared level cooldowns -- which is exactly the case where plain k-fold leaks
the answer into the training set and reports a model that does not exist out
of sample. Nine version bumps and twenty retrains are also twenty draws from
one urn, and nothing currently prices that.

Four tools, all pure:

  * `purged_kfold` -- k-fold that removes training samples whose label
    resolves inside the test window, plus an embargo after it
  * `walk_forward_splits` -- expanding window, trains only on the past
  * `deflated_sharpe` -- the probability a Sharpe survives the number of
    configurations tried to find it
  * `probability_of_backtest_overfitting` -- how often the in-sample winner
    underperforms out of sample

None of these decide anything. They are the evidence a human needs before a
model is allowed near an order.
"""
from __future__ import annotations

import itertools
import math
from statistics import NormalDist
from typing import Sequence

_N = NormalDist()
EULER_MASCHERONI = 0.5772156649015329

Span = tuple[float, float]
Split = tuple[list[int], list[int]]


def purged_kfold(n: int, k: int, spans: Sequence[Span],
                 embargo: float = 0.0) -> list[Split]:
    """k contiguous test folds, with leakage purged from each training set.

    `spans[i]` is `(t_start, t_end)` for sample i: when the observation began
    and when its label was finally known. A training sample is dropped when
    its span touches the test window at all, because training on it means
    training on information the test period had not yet produced.

    `embargo` extends the exclusion window forward in the same time units.
    Serial correlation does not stop at a fold boundary: without it, the
    first samples after the test window are near-copies of its last ones.
    """
    if k < 2 or k > n:
        raise ValueError(f"cannot make {k} folds from {n} samples")
    if len(spans) != n:
        raise ValueError("one span per sample is required")

    bounds = [round(i * n / k) for i in range(k + 1)]
    out: list[Split] = []
    for f in range(k):
        test = list(range(bounds[f], bounds[f + 1]))
        if not test:
            continue
        t_start = min(spans[i][0] for i in test)
        t_end = max(spans[i][1] for i in test) + embargo
        train = [i for i in range(n) if i not in set(test)
                 and not (spans[i][1] >= t_start and spans[i][0] <= t_end)]
        out.append((train, test))
    return out


def walk_forward_splits(n: int, folds: int) -> list[Split]:
    """Expanding-window splits: every test block is strictly after its
    training data, and the training window grows.

    The honest shape for a live system, because live is the only regime in
    which the past is all you have. The final test block absorbs any
    remainder rather than leaving the newest samples untested.
    """
    if folds < 1 or n < folds + 1:
        raise ValueError(f"cannot make {folds} walk-forward folds from {n}")
    block = n // (folds + 1)
    out: list[Split] = []
    for i in range(1, folds + 1):
        start = block * i
        end = n if i == folds else block * (i + 1)
        test = list(range(start, end))
        if test:
            out.append((list(range(0, start)), test))
    return out


def expected_max_sharpe(n_trials: int) -> float:
    """The Sharpe you would expect from the BEST of `n_trials` strategies
    that all have no edge at all, assuming unit variance across trials.

    This is the benchmark a real Sharpe has to clear. One trial clears
    nothing and returns 0: there was no selection, so there is nothing to
    deflate.
    """
    if n_trials <= 1:
        return 0.0
    g = EULER_MASCHERONI
    return ((1 - g) * _N.inv_cdf(1 - 1.0 / n_trials)
            + g * _N.inv_cdf(1 - 1.0 / (n_trials * math.e)))


def deflated_sharpe(sharpe: float, n_obs: int, n_trials: int,
                    skew: float = 0.0, kurtosis: float = 3.0) -> float:
    """Probability the observed Sharpe is real, given how many were tried.

    `sharpe` is PER OBSERVATION, not annualised -- mixing the two makes the
    benchmark meaningless, and the benchmark is the entire point. Returns a
    probability, so 0.95 is the usual bar and anything near 0.5 says the
    result is indistinguishable from the best of a pile of coin flips.
    """
    if n_obs < 2:
        return 0.0
    sr0 = expected_max_sharpe(n_trials)
    denom_sq = 1.0 - skew * sharpe + ((kurtosis - 1.0) / 4.0) * sharpe ** 2
    if denom_sq <= 0:
        return 0.0
    z = (sharpe - sr0) * math.sqrt(n_obs - 1) / math.sqrt(denom_sq)
    # Not rounded: deep in the tail the interesting differences are smaller
    # than any sensible number of decimal places, and a rounded 0.0 for two
    # very different pieces of evidence reads as "the same".
    return _N.cdf(z)


def probability_of_backtest_overfitting(matrix: Sequence[Sequence[float]],
                                        chunks: int = 8) -> float:
    """How often the in-sample best configuration lands in the losing half
    out of sample.

    Combinatorially symmetric cross-validation: split the periods into
    `chunks` contiguous blocks, take every way of using half of them in
    sample, pick the winner there, and see where it ranks on the other half.
    A number near 0.5 says the selection process is choosing noise, which is
    the failure mode a single impressive backtest cannot reveal.

    `matrix[p][c]` is configuration c's performance in period p.
    """
    n_periods = len(matrix)
    if n_periods < chunks or chunks < 2 or chunks % 2:
        raise ValueError(
            f"need an even chunk count of at least 2 and no more than the "
            f"{n_periods} periods available")
    n_cfg = len(matrix[0]) if matrix else 0
    if n_cfg < 2:
        raise ValueError("at least two configurations are needed to overfit")

    bounds = [round(i * n_periods / chunks) for i in range(chunks + 1)]
    blocks = [list(range(bounds[i], bounds[i + 1])) for i in range(chunks)]

    below_median = 0
    combos = 0
    for pick in itertools.combinations(range(chunks), chunks // 2):
        is_rows = [r for b in pick for r in blocks[b]]
        os_rows = [r for b in range(chunks) if b not in pick for r in blocks[b]]
        if not is_rows or not os_rows:
            continue
        combos += 1
        is_perf = [sum(matrix[r][c] for r in is_rows) / len(is_rows)
                   for c in range(n_cfg)]
        os_perf = [sum(matrix[r][c] for r in os_rows) / len(os_rows)
                   for c in range(n_cfg)]
        best = is_perf.index(max(is_perf))
        worse = sum(1 for c in range(n_cfg) if os_perf[c] < os_perf[best])
        # Relative rank in (0,1); below 0.5 means the in-sample winner is in
        # the bottom half out of sample.
        if (worse + 1) / (n_cfg + 1) < 0.5:
            below_median += 1

    return round(below_median / combos, 6) if combos else 0.0


def uniqueness_weights(spans: Sequence[Span]) -> list[float]:
    """Average uniqueness per sample: how much of its life it had to itself.

    A sample whose label resolves while five others are open is not an
    independent observation of anything, and a fit that counts it as one is
    confident about a sample it does not have. Up to six reversal signals can
    be open at once (`_MAX_OPEN_SIGNALS`), so this is not a theoretical
    concern here.

    For each sample, the mean of 1/(number of samples live) over the moments
    it was live. A zero-length span is measured at its own instant, which
    keeps a signal that opened and closed on one tick from vanishing from the
    fit entirely.
    """
    n = len(spans)
    if n == 0:
        return []

    edges = sorted({t for s in spans for t in s})
    out = []
    for start, end in spans:
        if end <= start:
            live = sum(1 for a, b in spans if a <= start <= b)
            out.append(1.0 / max(1, live))
            continue
        total = 0.0
        covered = 0.0
        points = [t for t in edges if start <= t <= end]
        if points and points[0] > start:
            points.insert(0, start)
        if not points or points[-1] < end:
            points.append(end)
        for a, b in zip(points, points[1:]):
            if b <= a:
                continue
            mid = (a + b) / 2.0
            live = sum(1 for s0, s1 in spans if s0 <= mid <= s1)
            total += (b - a) / max(1, live)
            covered += (b - a)
        out.append(total / covered if covered > 0 else 1.0)
    return out
