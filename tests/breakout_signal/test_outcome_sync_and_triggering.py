"""What `_check_outcomes` decides every five seconds.

This loop is where a Breakout signal becomes a triggered position, where a
live position's real MT5 result is copied back, and where a stale signal is
expired. It was almost entirely untested, and two of its decisions are not
reporting:

* **`_execute_live` is called from here**, so the pending->triggered step is
  a real order path. The guard that stops it -- `vantage_signal_id` already
  set, meaning the legs were staged at signal creation -- is the only thing
  between a grid-template signal and a SECOND grid on the same setup.
* **The win/loss it writes becomes the ML label.** `_close_and_learn` feeds
  the model. A misread outcome here does not produce a wrong number on a
  panel, it trains the engine on a fiction.

**No test reaches a broker or a database.** The bridge, the repo, the main
DB thread hop and `_close_and_learn` / `_manage_triggered_signal` /
`_execute_live` are all recorders, so a signal that got past every guard
records the attempt instead of placing anything.

One mutant here survives and is left surviving deliberately. Replacing
`if _mrow:` with `if True:` changes nothing observable: the whole sync
block sits inside a bare `except Exception`, so the `None` row raises,
the exception is swallowed at debug level, and the signal falls through
exactly as the guard would have made it. That equivalence IS the finding
-- a real failure to read the broker's close (a DB error, a schema
change, a wrong ticket) is indistinguishable from "the position is still
open", on the path that writes the ML label. Recorded in
`docs/system/domains/engines/README.md`; not changed here, because
changing it changes what gets learned from a live trade.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.breakout_signal import breakout_signal_service as svc

_NOW = 1_758_000_000.0


class _Tick:
    def __init__(self, bid=4002.0, ask=4002.6):
        self.bid = bid
        self.ask = ask


class _Bridge:
    def __init__(self, tick):
        self._tick = tick
        self.tick_calls = 0

    async def get_tick(self):
        self.tick_calls += 1
        return self._tick


class _Repo:
    def __init__(self):
        self.open_signals = []
        self.triggered = []
        self.expired = []
        self.exec_results = []
        self.main_close = None

    def get_open_signals(self):
        return [dict(s) for s in self.open_signals]

    def get_signal_by_id(self, sig_id):
        for s in self.open_signals:
            if s["id"] == sig_id:
                return dict(s)
        return None

    def trigger_signal(self, sig_id, price):
        self.triggered.append((sig_id, price))

    def expire_signal(self, sig_id, reason):
        self.expired.append((sig_id, reason))

    def update_live_exec_result(self, sig_id, ticket, vsid, status):
        self.exec_results.append((sig_id, ticket, vsid, status))

    def fetch_main_close_ro(self, ticket):
        return self.main_close


class _Engine(svc.BreakoutEngine):
    def __init__(self, bridge):
        self._bridge = bridge
        self.closed = []
        self.managed = []
        self.executed = []
        self.refreshes = 0

    def _compute_cost_pts(self, spread_raw):
        return 0.6

    def _close_and_learn(self, sig_id, close_px, outcome, reason,
                         entry, direction, lot, cost_pts):
        self.closed.append({"sig_id": sig_id, "close_px": close_px,
                            "outcome": outcome, "reason": reason,
                            "entry": entry, "direction": direction})

    def _manage_triggered_signal(self, sig, bid, ask, now, cost_pts):
        self.managed.append(sig["id"])

    async def _execute_live(self, sig, price, tick):
        self.executed.append((sig["id"], price))

    def _notify_refresh(self):
        self.refreshes += 1


def _pending(**kw):
    base = {"id": 1, "signal_ref": "BO-0001", "direction": "BUY",
            "status": "pending", "created_at": _NOW - 600,
            "entry_mid": 4002.0, "lot_size": 0.10}
    base.update(kw)
    return base


def _triggered(**kw):
    kw.setdefault("trigger_time", _NOW - 30)
    return _pending(status="triggered", **kw)


@pytest.fixture
def lab(monkeypatch):
    repo = _Repo()
    state = {"repo": repo, "tick": _Tick(), "session": (True, "London")}

    async def _to_db_thread(fn):
        return fn()

    monkeypatch.setattr(svc, "bdb", repo)
    monkeypatch.setattr(svc.time, "time", lambda: _NOW)
    monkeypatch.setattr("backend.src.db.database.to_db_thread", _to_db_thread)
    monkeypatch.setattr("backend.src.db.database.is_session_allowed",
                        lambda: state["session"])
    return state


def _run(state):
    engine = _Engine(_Bridge(state["tick"]))
    asyncio.run(engine._check_outcomes())
    return engine, state["repo"]


def _run_with_bridge(state):
    bridge = _Bridge(state["tick"])
    engine = _Engine(bridge)
    asyncio.run(engine._check_outcomes())
    return engine, state["repo"], bridge


# ── Nothing to do ────────────────────────────────────────────────────────────

def test_no_open_signals_asks_the_bridge_for_nothing(lab):
    """The early return is not cosmetic: this loop runs every five seconds,
    and with nothing open there is nothing a price could change."""
    engine, repo, bridge = _run_with_bridge(lab)

    assert bridge.tick_calls == 0
    assert repo.triggered == []
    assert engine.managed == []


def test_no_price_means_no_decision_is_taken_on_a_guess(lab):
    lab["tick"] = None
    lab["repo"].open_signals = [_pending()]

    engine, repo = _run(lab)

    assert repo.triggered == []
    assert engine.executed == []


# ── Triggering a pending signal ──────────────────────────────────────────────

def test_a_pending_buy_triggers_at_the_ask(lab):
    """A BUY is filled at the ask. Triggering it at the bid would book the
    spread as free profit on every signal."""
    lab["repo"].open_signals = [_pending()]

    engine, repo = _run(lab)

    assert repo.triggered == [(1, 4002.6)]


def test_a_pending_sell_triggers_at_the_bid(lab):
    lab["repo"].open_signals = [_pending(direction="SELL")]

    engine, repo = _run(lab)

    assert repo.triggered == [(1, 4002.0)]


def test_triggering_dispatches_the_live_order(lab):
    lab["repo"].open_signals = [_pending()]

    engine, _ = _run(lab)

    assert engine.executed == [(1, 4002.6)]


def test_a_switched_off_session_holds_the_signal_pending(lab):
    lab["session"] = (False, "Asian")
    lab["repo"].open_signals = [_pending()]

    engine, repo = _run(lab)

    assert repo.triggered == []
    assert engine.executed == []


class TestTheDoubleGridGuard:
    """A grid template stages its legs at signal creation. When the signal
    later triggers, executing again would open a second grid on the same
    setup -- the resting legs are already on the broker's book."""

    def test_a_signal_already_staged_is_not_executed_again(self, lab):
        lab["repo"].open_signals = [_pending(vantage_signal_id=7788)]

        engine, repo = _run(lab)

        assert engine.executed == []

    def test_it_is_still_marked_triggered(self, lab):
        """Skipping the second dispatch must not skip the state change, or
        the signal is re-triggered on every pass."""
        lab["repo"].open_signals = [_pending(vantage_signal_id=7788)]

        engine, repo = _run(lab)

        assert repo.triggered == [(1, 4002.6)]


# ── Copying the real MT5 result back ─────────────────────────────────────────

class TestTheLiveClosureSync:

    def _live(self, **kw):
        return _triggered(mt5_ticket=55501, live_exec_status="success", **kw)

    def test_a_profitable_close_is_learned_as_a_win(self, lab):
        lab["repo"].open_signals = [self._live()]
        lab["repo"].main_close = {"mt5_profit": 42.5, "net_pnl": 42.5}

        engine, _ = _run(lab)

        assert engine.closed[0]["outcome"] == "win"

    def test_a_losing_close_is_learned_as_a_loss(self, lab):
        lab["repo"].open_signals = [self._live()]
        lab["repo"].main_close = {"mt5_profit": -18.0, "net_pnl": -18.0}

        engine, _ = _run(lab)

        assert engine.closed[0]["outcome"] == "loss"

    def test_a_dollar_either_way_is_breakeven_not_a_win(self, lab):
        """The deadband exists because a scratch close is not evidence about
        the setup, and labelling it a win teaches the model it was."""
        lab["repo"].open_signals = [self._live()]
        lab["repo"].main_close = {"mt5_profit": 0.4, "net_pnl": 0.4}

        engine, _ = _run(lab)

        assert engine.closed[0]["outcome"] == "be"

    def test_a_small_LOSS_is_also_breakeven(self, lab):
        """The deadband is two-sided. Reading -$0.40 as a loss teaches the
        model the setup failed when it never got going."""
        lab["repo"].open_signals = [self._live()]
        lab["repo"].main_close = {"mt5_profit": -0.4, "net_pnl": -0.4}

        engine, _ = _run(lab)

        assert engine.closed[0]["outcome"] == "be"

    def test_the_reason_carries_the_real_broker_figure(self, lab):
        lab["repo"].open_signals = [self._live()]
        lab["repo"].main_close = {"mt5_profit": -18.0, "net_pnl": -18.0}

        engine, _ = _run(lab)

        assert "mt5_profit=-18.00" in engine.closed[0]["reason"]

    def test_a_still_open_position_is_not_closed(self, lab):
        lab["repo"].open_signals = [self._live()]
        lab["repo"].main_close = None

        engine, _ = _run(lab)

        assert engine.closed == []

    def test_a_live_position_is_never_managed_virtually(self, lab):
        """Its stops live on the broker. Running the virtual manager over it
        would close a real position on a simulated rule."""
        lab["repo"].open_signals = [self._live()]
        lab["repo"].main_close = None

        engine, _ = _run(lab)

        assert engine.managed == []

    def test_a_virtual_signal_IS_managed(self, lab):
        lab["repo"].open_signals = [_triggered()]

        engine, _ = _run(lab)

        assert engine.managed == [1]


# ── Housekeeping ─────────────────────────────────────────────────────────────

def test_a_signal_past_the_age_limit_is_expired(lab):
    lab["repo"].open_signals = [_pending(created_at=_NOW - svc._MAX_SIGNAL_AGE - 1)]

    engine, repo = _run(lab)

    assert repo.expired and repo.expired[0][0] == 1
    assert repo.triggered == [], "an expired signal must not also trigger"


def test_a_signal_inside_the_age_limit_is_not_expired(lab):
    lab["repo"].open_signals = [_pending(created_at=_NOW - svc._MAX_SIGNAL_AGE + 60)]

    engine, repo = _run(lab)

    assert repo.expired == []


def test_a_trigger_with_no_execution_response_is_marked_orphaned(lab):
    """Triggered, but nothing ever wrote back what the broker did. Left
    unmarked it looks like a working live trade forever."""
    lab["repo"].open_signals = [_triggered(trigger_time=_NOW - 200,
                                           live_exec_status=None)]

    engine, repo = _run(lab)

    assert repo.exec_results == [(1, None, None, "failed:orphaned_no_response")]


def test_a_deliberately_skipped_signal_is_not_relabelled_orphaned(lab):
    """"Orphaned" means nothing ever wrote back. A signal the gates refused
    DID write back -- calling that an orphan hides a working refusal behind
    a fault."""
    lab["repo"].open_signals = [_triggered(trigger_time=_NOW - 600,
                                           live_exec_status="skipped:live_off")]

    engine, repo = _run(lab)

    assert repo.exec_results == []


def test_a_recent_trigger_is_given_time_to_respond(lab):
    lab["repo"].open_signals = [_triggered(trigger_time=_NOW - 30,
                                           live_exec_status=None)]

    engine, repo = _run(lab)

    assert repo.exec_results == []
