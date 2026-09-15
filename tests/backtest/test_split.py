"""The in-sample / out-of-sample split -- docs/todo/003.

The template backtest reports one number per strategy over the whole loaded
window, and the constants that number validates were chosen by looking at that
same window. `engine.py:33-39` says `+0.167R/trade at 88.7% win rate`,
measured on the 259 signals that picked `_GDVR_SL_MULT = 4.0`. Part of that
figure is the strategy and part of it is the choosing, and nothing in the
report separates them.

These tests pin the separation, not the verdict. What the split says about any
particular template is not this file's business; that it partitions honestly
is.

The fixture is a 20-bar sawtooth (4000 -> 4006 -> 4000) repeated once per
signal, with each signal created at a cycle bottom so it fills on its own bar
and then rides the up-leg. Every trade is a win. That is deliberate: it makes
the in-sample half strongly profitable, which is exactly the condition under
which a side continued from the other side's closing balance would show
inflated lot sizes (test 3).
"""
from __future__ import annotations

import ast
import random
from pathlib import Path

import pytest

from backend.src.services.backtest import engine as bt
from backend.src.services.backtest import split as bt_split


# ── Fixture ───────────────────────────────────────────────────────────────────

_T0        = 1_700_000_000        # broker-clock ts of candle 0
_BAR_S     = 60
_CYCLE     = 20                   # bars per up/down cycle
_BALANCE   = 10_000.0
_RISK_PCT  = 2.0
_STRATEGY  = "conservative"
_N_CYCLES  = 24                   # fixed: every run walks the SAME series


def _bars(n_cycles: int) -> list[dict]:
    """A sawtooth: ten bars up from 4000 to 4006, ten back down."""
    out: list[dict] = []
    for _ in range(n_cycles):
        for j in range(10):
            out.append(4000.0 + j * 0.6)
        for j in range(10):
            out.append(4006.0 - j * 0.6)
    return [{"ts": _T0 + i * _BAR_S, "open": px, "high": px + 0.3,
             "low": px - 0.3, "close": px}
            for i, px in enumerate(out)]


def _sig(i: int, ts: float | None = None) -> bt.BtSignal:
    """Signal i, created (in true UTC) at the bottom of cycle i."""
    if ts is None:
        ts = _T0 - bt._BROKER_TZ_OFFSET + (_CYCLE * i) * _BAR_S
    return bt.BtSignal(
        signal_id=f"s{i}", direction="BUY",
        entry_low=3999.5, entry_high=4000.5, stop_loss=3990.0,
        tp1=4003.0, tp2=4005.0, tp3=None,
        created_ts=float(ts), source="fixture",
    )


def _run(signals, *, split_fraction=0.0, **kw):
    return bt.run_backtest(
        signals            = signals,
        candles            = _bars(_N_CYCLES),
        strategies         = [_STRATEGY],
        starting_balance   = _BALANCE,
        risk_pct           = _RISK_PCT,
        spread_pts         = 0.4,
        commission_per_lot = 0.0,
        split_fraction     = split_fraction,
        **kw,
    )[_STRATEGY]


def _ids(stats) -> set[str]:
    return {t.signal_id for t in stats.trade_list}


# ── 1. Partition is by created_ts, never by list position ────────────────────

def test_partition_sorts_by_created_ts_not_list_order():
    """A slicing implementation passes on sorted input and fails here.

    Negative control: `repo.fetch_backtest_signals` orders by `created_at`, so
    a list-position slice looks correct against every DB-sourced run. Manual
    signals do not arrive sorted. Shuffling is the only thing that tells the
    two implementations apart.
    """
    ordered  = [_sig(i) for i in range(10)]
    shuffled = ordered[:]
    random.Random(7).shuffle(shuffled)
    assert [s.signal_id for s in shuffled] != [s.signal_id for s in ordered], \
        "fixture is not actually shuffled -- the test would pass vacuously"

    a_is, a_oos, a_frac = bt_split.partition(ordered,  0.5)
    b_is, b_oos, b_frac = bt_split.partition(shuffled, 0.5)

    assert [s.signal_id for s in a_is]  == [s.signal_id for s in b_is]
    assert [s.signal_id for s in a_oos] == [s.signal_id for s in b_oos]
    assert a_frac == b_frac == 0.5


# ── 2. No split requested means nothing changed ──────────────────────────────

def test_no_split_is_identical_to_today():
    sigs = [_sig(i) for i in range(8)]

    without = _run(sigs)
    zero    = _run(sigs, split_fraction=0.0)

    assert without == zero
    assert without.split is None


def test_the_fixture_can_tell_the_two_paths_apart():
    """The capability control for the test above.

    Equality alone would also hold against a `split_fraction` that was parsed
    and then ignored. This asserts the fixture is one where requesting a split
    visibly changes the result.
    """
    sigs = [_sig(i) for i in range(8)]

    assert _run(sigs, split_fraction=0.0) != _run(sigs, split_fraction=0.5,
                                                  split_min_trades=1)


# ── 3. Each side is its own account ──────────────────────────────────────────

def test_each_side_starts_from_the_same_balance():
    sigs = [_sig(i) for i in range(12)]
    got  = _run(sigs, split_fraction=0.5, split_min_trades=1)

    assert got.split is not None
    oos = got.split.out_of_sample
    assert oos is not None
    assert oos.equity_curve[0] == pytest.approx(_BALANCE)


def test_out_of_sample_lots_match_a_standalone_run_of_those_signals():
    """The assertion that actually catches a continued balance.

    `equity_curve[0]` alone does not: an implementation could reset the curve
    for display and still have sized every lot off the in-sample closing
    balance. Lot size is computed from equity on every trade, so comparing
    lots against a run of the same signals alone is what pins it.

    The fixture's in-sample half is all winners, so a contaminated run sizes
    the out-of-sample half off a much larger balance -- far beyond the 2dp
    rounding in `_lot_size`.
    """
    sigs = [_sig(i) for i in range(12)]
    got  = _run(sigs, split_fraction=0.5, split_min_trades=1)

    oos_sigs = bt_split.partition(sigs, 0.5)[1]
    alone    = _run(oos_sigs)

    assert [t.lot_size for t in got.split.out_of_sample.trade_list] == \
           [t.lot_size for t in alone.trade_list]

    # And the fixture really does move the balance, or the comparison is empty.
    assert got.split.in_sample.final_balance > _BALANCE * 1.05


# ── 4. Every filled signal lands on exactly one side ─────────────────────────

def test_sides_partition_the_filled_signals_exactly():
    """Set equality, not count equality.

    `len(is) + len(oos) == len(combined)` also holds for an off-by-one that
    moves one signal across the boundary. Fill detection depends on price and
    not on balance, and `_lot_size` clamps to `_MIN_LOT` rather than zero, so
    the same signals fill on every run -- which is what makes set equality the
    right assertion here.
    """
    sigs     = [_sig(i) for i in range(12)]
    got      = _run(sigs, split_fraction=0.5, split_min_trades=1)
    combined = _ids(got)

    in_s  = _ids(got.split.in_sample)
    out_s = _ids(got.split.out_of_sample)

    assert in_s | out_s == combined
    assert in_s & out_s == set()
    assert combined, "fixture filled nothing -- the test would pass vacuously"


# ── 5. A tie never straddles the boundary ────────────────────────────────────

def test_tied_timestamps_never_straddle_the_boundary():
    """Three signals share one creation time, placed exactly on the cut.

    A naive index cut splits them, which would put two signals the tuner saw
    together on opposite sides of a line that claims to separate what it saw
    from what it did not.
    """
    sigs = [_sig(i) for i in range(10)]
    tied = sigs[4].created_ts
    sigs[5] = _sig(5, ts=tied)
    sigs[6] = _sig(6, ts=tied)

    in_s, out_s, frac = bt_split.partition(sigs, 0.5)
    ids_in = {s.signal_id for s in in_s}

    assert {"s4", "s5", "s6"} <= ids_in, "the tie was split"
    assert {s.signal_id for s in out_s} == {"s7", "s8", "s9"}
    assert frac == pytest.approx(0.7)
    assert frac != pytest.approx(0.5), \
           "achieved fraction still reports the request, not what happened"


def test_boundary_ts_is_the_first_out_of_sample_signal():
    sigs = [_sig(i) for i in range(10)]
    got  = _run(sigs, split_fraction=0.5, split_min_trades=1)

    assert got.split.boundary_ts == sigs[5].created_ts


# ── 6. A side too thin to mean anything says so ──────────────────────────────

@pytest.mark.parametrize("min_trades, measured", [(5, True), (6, True), (7, False)])
def test_a_thin_side_is_noted_not_numbered(min_trades, measured):
    """Twelve signals, split in half, is six trades a side.

    Parametrised across the boundary: at min_trades=6 the side is exactly at
    the threshold and must be measured. That case is the one that fails if the
    comparison is `>` instead of `>=`.
    """
    sigs = [_sig(i) for i in range(12)]
    got  = _run(sigs, split_fraction=0.5, split_min_trades=min_trades)

    assert got.split.in_sample is not None or not measured
    if measured:
        assert got.split.out_of_sample is not None
        assert got.split.out_of_sample_note == ""
    else:
        assert got.split.out_of_sample is None
        note = got.split.out_of_sample_note
        assert "6" in note and str(min_trades) in note
        assert "036" in note, "the note must point at the open owner decision"


def test_the_provisional_minimum_is_twenty():
    """Provisional, and the handover file says so. If this changes, the file
    at docs/simon-handover/036 is where the decision is recorded."""
    assert bt_split.MIN_TRADES_PER_SIDE == 20


# ── 7. The split reaches nothing that can move money ─────────────────────────

def test_split_module_imports_nothing_that_can_trade():
    """Static, so it fails on the import rather than at some later call.

    A later change reaching for a live price, a real lot-size helper or the DB
    would make the split's numbers depend on the state of the running system
    instead of the signals handed to it.
    """
    src  = Path(bt_split.__file__).read_text(encoding="utf-8")
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
        assert not any(b in mod for b in banned), f"split.py imports {mod}"


# ── 8. A refused template is refused, not split ──────────────────────────────

def test_a_refused_template_gets_no_split():
    """`unsupported_reason` and the split are two different silences.

    Splitting a template the walk refused produces two halves of nothing, and
    two rows of zeros read worse than one. The refusal is what the row shows.
    See tests/backtest/test_unsupported_template_reason.py for why zeros in a
    comparison table are an argument FOR the strategy that was never run.
    """
    sigs = [_sig(i) for i in range(12)]
    got  = _run(sigs, split_fraction=0.5, split_min_trades=1)
    got.unsupported_reason = "pretend refusal"

    refused = bt_split.split_stats(
        stats=got, signals=sigs, fraction=0.5, min_trades=1,
        run_side=lambda subset: got,
    )
    assert refused is None
