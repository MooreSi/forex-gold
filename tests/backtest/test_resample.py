"""Resampling the trade sequence -- docs/todo/backtest/020.

The backtest reports ONE equity path: the trades in the order they happened.
Max drawdown is the number that suffers most from that, because it depends on
where the losing runs fell. A strategy whose losses happened to be spread out
reports a shallow drawdown and looks safe.

These tests pin the resampling, not the verdict. What the band says about any
particular template is not this file's business; that it is computed honestly
is.

Two facts drive most of what follows and are worth stating once:

  * **A shuffle cannot change the final balance.** Compounding multiplies
    (1 + f) over the trades and addition adds dollars; both are commutative.
    Only the PATH moves, which is exactly why the shuffle is the honest
    estimator for drawdown and says nothing about profit. The bootstrap, which
    draws with replacement, is what moves the final balance.
  * **The per-trade step is a fraction under compounding and dollars under
    fixed lots.** `run_backtest` re-sizes on current equity after every trade,
    so a trade's dollar P&L scales with the balance it was opened on. Replaying
    fixed dollars would be resampling a different system. `test_round_trip_*`
    is the test that proves this was actually done.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.src.services.backtest import engine as bt
from backend.src.services.backtest import resample as rs


# ── Fixtures ──────────────────────────────────────────────────────────────────

_BALANCE = 10_000.0


def _trade(pnl: float, idx: int = 0) -> bt.BtTrade:
    """A BtTrade carrying nothing but the P&L the resampler reads."""
    return bt.BtTrade(
        signal_id=f"s{idx}", strategy="conservative", direction="buy",
        fill_price=4000.0, fill_bar_idx=idx, lot_size=0.10, pnl_usd=pnl,
    )


def _stats(pnls: list[float], balance: float = _BALANCE) -> bt.StrategyStats:
    """Run the real `_compute_stats`, so equity_curve and max_drawdown_pct are
    produced by the code under comparison rather than by this file."""
    return bt._compute_stats("conservative",
                             [_trade(p, i) for i, p in enumerate(pnls)],
                             balance)


def _compounding_pnls(fractions: list[float], balance: float = _BALANCE) -> list[float]:
    """Dollar P&Ls that a compounding account would actually have produced from
    the given per-trade return fractions. Without this the fixture's dollars
    and its equity curve disagree and the round-trip test proves nothing."""
    out, bal = [], balance
    for f in fractions:
        pnl = bal * f
        out.append(pnl)
        bal += pnl
    return out


# ── 1. Round trip: the steps must rebuild the realised path ──────────────────

def test_round_trip_reproduces_the_realised_path_when_compounding():
    """The whole module rests on this. If the steps do not rebuild the path
    the run actually took, every band around them is measuring some other
    system."""
    fractions = [0.05, -0.02, 0.03, -0.04, 0.06, -0.01, 0.02, -0.03]
    stats = _stats(_compounding_pnls(fractions))

    steps, sizing = rs.trade_steps(stats, _BALANCE, lots_per_trade=0.0)
    final, max_dd = rs.replay(steps, _BALANCE, sizing)

    assert sizing == rs.COMPOUNDING
    assert final == pytest.approx(stats.final_balance, rel=1e-9)
    assert max_dd == pytest.approx(stats.max_drawdown_pct, abs=1e-6)


def test_round_trip_reproduces_the_realised_path_when_lots_are_fixed():
    pnls  = [500.0, -200.0, 300.0, -400.0, 600.0, -100.0]
    stats = _stats(pnls)

    steps, sizing = rs.trade_steps(stats, _BALANCE, lots_per_trade=0.10)
    final, max_dd = rs.replay(steps, _BALANCE, sizing)

    assert sizing == rs.FIXED
    assert steps == pytest.approx(pnls)
    assert final == pytest.approx(stats.final_balance, rel=1e-9)
    assert max_dd == pytest.approx(stats.max_drawdown_pct, abs=1e-6)


def test_compounding_steps_are_fractions_of_running_equity_not_of_the_start():
    """The negative control for the test above: a resampler that divided every
    P&L by the STARTING balance would also produce plausible-looking fractions
    and would round-trip wrongly on a run that grew."""
    fractions = [0.20, 0.20, 0.20]
    pnls      = _compounding_pnls(fractions)
    stats     = _stats(pnls)

    steps, _ = rs.trade_steps(stats, _BALANCE, lots_per_trade=0.0)

    assert steps == pytest.approx([0.20, 0.20, 0.20])
    # The naive version. The third trade earned $2,880 on a $14,400 balance;
    # divided by the start it reads as 28.8%.
    assert steps[2] != pytest.approx(pnls[2] / _BALANCE)


# ── 2. A shuffle moves the path, never the destination ───────────────────────

def test_a_shuffle_leaves_the_final_balance_untouched():
    """Commutativity, asserted rather than assumed. If a band around the final
    balance ever appears under the shuffle method, the replay is lossy."""
    stats = _stats(_compounding_pnls([0.05, -0.02, 0.03, -0.04, 0.06, -0.01,
                                      0.02, -0.03, 0.04, -0.05] * 3))

    out = rs.resample(stats, _BALANCE, method=rs.SHUFFLE, draws=200, seed=1)

    assert out.final_balance.p5 == pytest.approx(out.final_balance.p95, rel=1e-9)
    assert out.final_balance.median == pytest.approx(stats.final_balance, rel=1e-9)


def test_a_bootstrap_does_move_the_final_balance():
    """The negative control for the test above. Drawing with replacement is a
    different question and must produce a different answer."""
    stats = _stats(_compounding_pnls([0.05, -0.02, 0.03, -0.04, 0.06, -0.01,
                                      0.02, -0.03, 0.04, -0.05] * 3))

    out = rs.resample(stats, _BALANCE, method=rs.BOOTSTRAP, draws=500, seed=1)

    assert out.final_balance.p5 < out.final_balance.p95


# ── 3. The drawdown band brackets the ordering ───────────────────────────────

def test_a_contiguous_losing_run_is_the_deepest_ordering_there_is():
    """Drawdown is multiplicative, so the decline from a peak is the product of
    the losing steps. Putting every loss together makes that product as small
    as it can be, and no ordering of the same trades can do worse. That holds
    whether the run comes first (the peak is the opening balance) or last (the
    peak is the top of the winning run) -- both give the identical figure, and
    this test asserts the pair rather than guessing which is worse.
    """
    first = _stats(_compounding_pnls([-0.03] * 12 + [0.05] * 12))
    last  = _stats(_compounding_pnls([0.05] * 12 + [-0.03] * 12))

    out_first = rs.resample(first, _BALANCE, method=rs.SHUFFLE, draws=500, seed=2)
    out_last  = rs.resample(last,  _BALANCE, method=rs.SHUFFLE, draws=500, seed=2)

    assert out_first.realised_max_drawdown_pct == pytest.approx(
        out_last.realised_max_drawdown_pct)
    assert out_first.realised_max_drawdown_pct > out_first.max_drawdown_pct.p95
    assert out_last.realised_max_drawdown_pct  > out_last.max_drawdown_pct.p95


def test_an_alternating_order_is_the_shallowest_and_the_band_says_so():
    """The mirror, and the case that matters in practice: an ordering that
    never strings two losses together reports a drawdown no realistic sequence
    would have produced. Without the band it reads as a safe strategy.
    """
    fractions = [0.05, -0.03] * 12
    stats     = _stats(_compounding_pnls(fractions))

    out = rs.resample(stats, _BALANCE, method=rs.SHUFFLE, draws=500, seed=2)

    assert out.realised_max_drawdown_pct < out.max_drawdown_pct.median


def test_the_realised_percentile_says_where_the_actual_run_sat():
    """Same two orderings, read through the one number a person would look at."""
    clustered   = _stats(_compounding_pnls([0.05] * 12 + [-0.03] * 12))
    alternating = _stats(_compounding_pnls([0.05, -0.03] * 12))

    hi = rs.resample(clustered,   _BALANCE, method=rs.SHUFFLE, draws=500, seed=2)
    lo = rs.resample(alternating, _BALANCE, method=rs.SHUFFLE, draws=500, seed=2)

    assert hi.realised_drawdown_percentile > 95.0
    assert lo.realised_drawdown_percentile < 50.0


def test_identical_steps_collapse_the_band_to_a_point():
    """No ordering can change a path whose steps are all the same, so the
    interval must have no width. A resampler that jittered here would be
    inventing variance."""
    stats = _stats(_compounding_pnls([0.02] * 25))

    out = rs.resample(stats, _BALANCE, method=rs.SHUFFLE, draws=200, seed=3)

    assert out.max_drawdown_pct.p5 == pytest.approx(out.max_drawdown_pct.p95)
    assert out.max_drawdown_pct.median == pytest.approx(0.0, abs=1e-9)


# ── 4. Determinism ───────────────────────────────────────────────────────────

def test_the_same_seed_reproduces_the_band_exactly():
    stats = _stats(_compounding_pnls([0.05, -0.02, 0.03, -0.04, 0.06,
                                      -0.01, 0.02, -0.03] * 4))

    a = rs.resample(stats, _BALANCE, method=rs.BOOTSTRAP, draws=300, seed=7)
    b = rs.resample(stats, _BALANCE, method=rs.BOOTSTRAP, draws=300, seed=7)

    assert a == b


def test_a_different_seed_gives_a_different_band():
    stats = _stats(_compounding_pnls([0.05, -0.02, 0.03, -0.04, 0.06,
                                      -0.01, 0.02, -0.03] * 4))

    a = rs.resample(stats, _BALANCE, method=rs.BOOTSTRAP, draws=300, seed=7)
    b = rs.resample(stats, _BALANCE, method=rs.BOOTSTRAP, draws=300, seed=8)

    assert a != b


# ── 5. Clustered trades are not independent, and the block method knows ──────

def test_the_block_bootstrap_is_wider_than_iid_on_clustered_trades():
    """Trades here are not i.i.d.: overlapping positions and one-way sessions
    mean losses arrive together. An i.i.d. bootstrap breaks those runs apart
    and reports a drawdown band that is too narrow -- the optimistic error.

    The fixture is six blocks of five same-sign trades. Drawing whole blocks
    preserves the runs; drawing singles destroys them.
    """
    fractions: list[float] = []
    for i in range(6):
        fractions += [0.04] * 5 if i % 2 == 0 else [-0.03] * 5
    stats = _stats(_compounding_pnls(fractions))

    iid   = rs.resample(stats, _BALANCE, method=rs.BOOTSTRAP, draws=800, seed=4)
    block = rs.resample(stats, _BALANCE, method=rs.BLOCK, draws=800, seed=4,
                        block_size=5)

    iid_width   = iid.max_drawdown_pct.p95 - iid.max_drawdown_pct.p5
    block_width = block.max_drawdown_pct.p95 - block.max_drawdown_pct.p5
    assert block_width > iid_width


# ── 6. A thin sample is noted, not numbered ──────────────────────────────────

def test_a_thin_sample_reports_a_note_and_no_band():
    """Follows split.py's precedent: a row of numbers computed from four
    trades reads as measurement, and beside a row showing real drawdown it is
    an argument FOR the strategy that was never measured."""
    stats = _stats(_compounding_pnls([0.05, -0.02, 0.03, -0.04]))

    out = rs.resample(stats, _BALANCE, method=rs.SHUFFLE, draws=200, seed=5)

    assert out.note != ""
    assert out.draws == 0
    assert out.max_drawdown_pct.median == 0.0


def test_the_minimum_matches_the_split_and_says_so():
    """Both are the same question -- how few trades is too few -- and the
    owner answers it once. docs/simon-handover/036."""
    from backend.src.services.backtest import split as bt_split

    assert rs.MIN_TRADES == bt_split.MIN_TRADES_PER_SIDE


# ── 7. No trades, no band ────────────────────────────────────────────────────

def test_a_strategy_that_never_traded_reports_a_note_not_zeros():
    stats = _stats([])

    out = rs.resample(stats, _BALANCE, method=rs.SHUFFLE, draws=200, seed=6)

    assert out.note != ""
    assert out.draws == 0


def test_an_unknown_method_is_refused_rather_than_silently_shuffled():
    stats = _stats(_compounding_pnls([0.02, -0.01] * 15))

    with pytest.raises(ValueError):
        rs.resample(stats, _BALANCE, method="montecarlo", draws=10, seed=1)


# ── 8. Purity ────────────────────────────────────────────────────────────────

def test_resample_module_imports_nothing_that_can_trade():
    """Static, so it fails on the import rather than at some later call."""
    src  = Path(rs.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    banned = ("services.trading", "services.broker", "services.risk",
              "backend.src.db", "MetaTrader5", "mt5")

    seen: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            seen += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            seen.append(node.module or "")

    for mod in seen:
        assert not any(b in mod for b in banned), f"resample.py imports {mod}"
