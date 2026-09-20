"""The P&L a closed Breakout signal is recorded with, and the label it teaches.

`_close_and_learn` is the only place a Breakout signal's money figure is
computed. Everything downstream reads what it writes: the panel, the edge
stats, and `ml_engine.record_outcome`, which is the training label. It was
28% covered.

**No test reaches a broker, a database or the ledger.** The repo, the ML
engine, the cross-engine bus and the cluster ledger push are all recorders.

Two behaviours here are **characterised, not endorsed**, and both are
reported rather than changed, because changing either changes what the
engine learns from real trades:

1. **The `outcome` parameter is dead.** `ml_outcome = outcome` is followed
   immediately by an exhaustive if/elif/else on `net_dol`, so the value
   every caller passes is discarded. `TestTheOutcomeArgumentIsDiscarded`
   states that.
2. **A live trade's label can come from the virtual calculation.**
   `_check_outcomes` reads the broker's real profit and passes the outcome
   it derived; this function ignores it, recomputes from the virtual close
   price, and only afterwards re-reads MT5 -- inside a `try/except` logged
   at debug. If that lookup fails, the recorded outcome is the virtual one,
   on a position that really traded.

The module docstring also records a known `close_signal` balance
double-counting bug, preserved deliberately by the extraction. Nothing here
asserts it is correct; these tests are about this function's own output.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.breakout_signal import breakout_signal_manage as mg

_NOW = 1_758_000_000.0


class _Repo:
    def __init__(self):
        self.signal = {"id": 1, "signal_ref": "BO-0001", "strategy": "conservative",
                       "created_at": _NOW - 3600, "remaining_frac": 1.0,
                       "partial_pnl_dollars": 0.0, "mt5_ticket": None}
        self.closed = []
        self.mt5_profit = None
        self.pnl_from_mt5 = []

    def get_signal_by_id(self, sig_id):
        return dict(self.signal)

    def close_signal(self, sig_id, close_price, outcome, note, **kw):
        self.closed.append({"sig_id": sig_id, "close_price": close_price,
                            "outcome": outcome, "note": note, **kw})

    def fetch_main_mt5_profit(self, ticket):
        return self.mt5_profit

    def update_signal_pnl_from_mt5(self, sig_id, profit, outcome):
        self.pnl_from_mt5.append((sig_id, profit, outcome))


class _Engine(mg._ManagementMixin):
    def __init__(self):
        self._closed_count = 0
        self.batch_runs = 0

    async def _run_batch_analysis(self):
        self.batch_runs += 1


@pytest.fixture
def lab(monkeypatch):
    repo = _Repo()
    state = {"repo": repo, "recorded": [], "ledger": [], "bus": []}

    monkeypatch.setattr(mg, "bdb", repo)
    monkeypatch.setattr(mg.time, "time", lambda: _NOW)
    monkeypatch.setattr(
        "backend.src.services.breakout_signal.ml_engine.record_outcome",
        lambda sig_id, outcome: state["recorded"].append((sig_id, outcome)))
    monkeypatch.setattr(
        "backend.src.services.cluster.sync.ledger.push_trade_closed",
        lambda payload: state["ledger"].append(dict(payload)))
    monkeypatch.setattr("backend.src.db.database.close_bus_entry",
                        lambda engine, sig_id: state["bus"].append((engine, sig_id)))
    return state


def _close(state, close_price=4012.0, outcome="win", entry=4002.0,
           direction="BUY", lot=0.10, cost_pts=0.6, engine=None):
    engine = engine or _Engine()
    engine._close_and_learn(1, close_price, outcome, "test close",
                            entry, direction, lot, cost_pts)
    return engine, state["repo"].closed[-1]


# ── The arithmetic ───────────────────────────────────────────────────────────

def test_a_buy_that_rose_is_a_profit(lab):
    """10 points, less 0.6 cost, at 0.10 lots x 100 = $94.00."""
    _, row = _close(lab, close_price=4012.0, entry=4002.0, direction="BUY")

    assert row["net_pnl_dollars"] == 94.00


def test_a_sell_that_rose_is_a_loss(lab):
    """Direction is not cosmetic: the same price move pays a BUY and costs
    a SELL. Getting this backwards reports every losing short as a winner."""
    _, row = _close(lab, close_price=4012.0, entry=4002.0, direction="SELL")

    assert row["net_pnl_dollars"] == -106.00


def test_a_sell_that_fell_is_a_profit(lab):
    _, row = _close(lab, close_price=3992.0, entry=4002.0, direction="SELL")

    assert row["net_pnl_dollars"] == 94.00


def test_the_round_trip_cost_is_taken_off(lab):
    """A flat close is not a flat result -- the spread, slippage and
    commission were still paid."""
    _, row = _close(lab, close_price=4002.0, entry=4002.0, cost_pts=0.6)

    assert row["net_pnl_dollars"] == -6.00


def test_the_lot_size_scales_the_result(lab):
    _, row = _close(lab, close_price=4012.0, entry=4002.0, lot=0.20)

    assert row["net_pnl_dollars"] == 188.00


class TestPartialsAlreadyBanked:

    def test_only_the_remaining_fraction_is_priced_at_the_close(self, lab):
        """Two thirds were closed earlier. The final leg is a third of the
        position, not all of it."""
        lab["repo"].signal["remaining_frac"] = 0.34
        lab["repo"].signal["partial_pnl_dollars"] = 0.0

        _, row = _close(lab, close_price=4012.0, entry=4002.0)

        assert row["net_pnl_dollars"] == 31.96

    def test_money_already_booked_is_added_not_recomputed(self, lab):
        lab["repo"].signal["remaining_frac"] = 0.34
        lab["repo"].signal["partial_pnl_dollars"] = 55.50

        _, row = _close(lab, close_price=4012.0, entry=4002.0)

        assert row["net_pnl_dollars"] == 87.46

    def test_a_missing_fraction_is_treated_as_the_whole_position(self, lab):
        lab["repo"].signal["remaining_frac"] = None

        _, row = _close(lab, close_price=4012.0, entry=4002.0)

        assert row["net_pnl_dollars"] == 94.00


# ── The label that gets taught ───────────────────────────────────────────────

def test_a_profit_is_learned_as_a_win(lab):
    _close(lab, close_price=4012.0, entry=4002.0)

    assert lab["recorded"] == [(1, "win")]


def test_a_loss_is_learned_as_a_loss(lab):
    _close(lab, close_price=3992.0, entry=4002.0)

    assert lab["recorded"] == [(1, "loss")]


def test_a_scratch_close_is_learned_as_breakeven(lab):
    """Fifty cents either way is noise, not evidence about the setup."""
    _close(lab, close_price=4002.64, entry=4002.0)

    assert lab["recorded"] == [(1, "be")]


def test_a_small_negative_is_breakeven_too(lab):
    """The deadband is two-sided, and the losing half needs its own case:
    with only a small PROFIT exercised, narrowing the loss threshold to zero
    goes unnoticed. A forty-cent loss is a scratch, not a failed setup."""
    _close(lab, close_price=4002.56, entry=4002.0)

    assert lab["repo"].closed[-1]["net_pnl_dollars"] == -0.40
    assert lab["recorded"] == [(1, "be")]


class TestTheOutcomeArgumentIsDiscarded:
    """`ml_outcome = outcome` is followed by an exhaustive if/elif/else on
    net_dol, so the caller's value never survives. Characterised, not
    endorsed -- reported in docs/system/domains/engines/README.md."""

    def test_a_caller_saying_win_on_a_losing_close_is_overruled(self, lab):
        _, row = _close(lab, close_price=3992.0, entry=4002.0, outcome="win")

        assert row["outcome"] == "loss"

    def test_a_caller_saying_loss_on_a_winning_close_is_overruled(self, lab):
        _, row = _close(lab, close_price=4012.0, entry=4002.0, outcome="loss")

        assert row["outcome"] == "win"

    def test_the_breakeven_a_caller_asks_for_is_also_recomputed(self, lab):
        """"be" passed on a clearly profitable close still reads win, so
        even the value a caller and this function would agree on is not the
        one that was used."""
        _, row = _close(lab, close_price=4012.0, entry=4002.0, outcome="be")

        assert row["outcome"] == "win"


# ── What the broker actually paid ────────────────────────────────────────────

class TestTheMt5Override:

    def test_the_real_profit_replaces_the_virtual_one(self, lab):
        lab["repo"].signal["mt5_ticket"] = 55501
        lab["repo"].mt5_profit = (-22.5,)

        _close(lab, close_price=4012.0, entry=4002.0)

        assert lab["repo"].pnl_from_mt5 == [(1, -22.5, "loss")]
        assert lab["recorded"] == [(1, "loss")], "the model must learn the real result"

    def test_a_virtual_signal_is_never_overridden(self, lab):
        lab["repo"].signal["mt5_ticket"] = None
        lab["repo"].mt5_profit = (-22.5,)

        _close(lab, close_price=4012.0, entry=4002.0)

        assert lab["repo"].pnl_from_mt5 == []
        assert lab["recorded"] == [(1, "win")]

    def test_a_failed_lookup_leaves_the_VIRTUAL_label_on_a_real_trade(self, lab):
        """Characterised, not endorsed. The position really traded, the
        broker's figure was already known to the caller and passed in, and
        a debug-level exception here means the model is taught the
        simulated result instead."""
        lab["repo"].signal["mt5_ticket"] = 55501

        def _boom(ticket):
            raise RuntimeError("main db unavailable")

        lab["repo"].fetch_main_mt5_profit = _boom

        _close(lab, close_price=4012.0, entry=4002.0, outcome="loss")

        assert lab["recorded"] == [(1, "win")]
        assert lab["repo"].pnl_from_mt5 == []


# ── Side effects ─────────────────────────────────────────────────────────────

def test_the_closed_trade_is_pushed_to_the_cluster_ledger(lab):
    _close(lab, close_price=4012.0, entry=4002.0)

    assert lab["ledger"][0]["engine"] == "breakout"
    assert lab["ledger"][0]["pnl_dollars"] == 94.00
    assert lab["ledger"][0]["tg_source"] == "Breakout Engine"


def test_the_signal_bus_entry_is_released(lab):
    """Left open, it blocks the other engine from taking the other side for
    the full six-hour TTL."""
    _close(lab, close_price=4012.0, entry=4002.0)

    assert lab["bus"] == [("breakout", 1)]


def test_a_broken_ledger_does_not_stop_the_close(lab, monkeypatch):
    monkeypatch.setattr(
        "backend.src.services.cluster.sync.ledger.push_trade_closed",
        lambda payload: (_ for _ in ()).throw(RuntimeError("peer offline")))

    _close(lab, close_price=4012.0, entry=4002.0)

    assert lab["recorded"] == [(1, "win")]


def test_batch_analysis_runs_every_tenth_close(lab):
    """Driven on an event loop because that is where it really runs.

    The batch review is scheduled with `asyncio.ensure_future` from a
    SYNCHRONOUS method. Its caller (`_check_outcomes`) is a coroutine, so a
    loop is always running in production -- but off a loop the coroutine is
    created and never executed, and the only symptom is a DeprecationWarning.
    Asserting it from a plain function proved nothing; this drives it the way
    the engine does.
    """
    async def _drive():
        engine = _Engine()
        engine._closed_count = 8

        _close(lab, engine=engine)
        await asyncio.sleep(0)
        assert engine.batch_runs == 0, "the ninth close must not trigger a review"

        _close(lab, engine=engine)
        await asyncio.sleep(0)
        return engine

    engine = asyncio.run(_drive())

    assert engine.batch_runs == 1
