"""Max Open Trades counts trades that are live, not orders resting at the broker.

Owner, 2026-09-28, reversing the 2026-09-04 rule ("whether it is a resting
order or a market order the EA should manage the max number of allowable
trades"):

    "max number of trades means maximum number of executed trades that are
     live on mt5 on the active node, excluding limit orders, a limit order
     cannot execute if there are already 3 live trades"

and, asked how: withdraw resting orders from the broker while the book is
full and re-arm them when a slot frees; count one app trade as one slot
however many legs it has at MT5.

So there are two halves, and this file pins both:

  * the COUNT -- a resting order (working or withdrawn) holds no slot. An open
    position does, and so does an open in flight, which is what stops two
    simultaneous market orders both passing the cap;
  * the GUARD -- MT5 cannot make a limit order conditional on a position
    count, so while the book is full every resting order is taken off it, and
    none is put back until a slot is free.

An open row the app has not yet matched to a fill (an EA Template placeholder,
ticket 0) still counts. It may be a market order filling right now, and
counting it for the few minutes the placeholder repair takes is the side that
cannot over-open.

Nothing here reaches a broker. The EA is a fake that records the two calls a
resting order can receive; it has no way to open or close a position, and
`TestItNeverCloses` checks the guard's own source names no close path.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import time

import pytest

from backend.src.db import database as db
from backend.src.services.trading import resting_revalidation as rr
from backend.src.services.trading import signal_state_repo as ssr


def _set_cap(n: int):
    with db.db() as conn:
        conn.execute("UPDATE vantage_risk_settings SET max_open_trades=? WHERE id=1", (n,))


def _signal(signal_id, status="pending"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,"
            "entry_low,entry_high,stop_loss,lot_size,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (signal_id, "Test", "BUY", 4000.0, 4002.0, 3990.0, 0.1, status, time.time()))


def _open_trade(trade_id, signal_id=None, ticket=111, entry=4000.0):
    signal_id = signal_id or f"sig-{trade_id}"
    _signal(signal_id, status="active")
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id,signal_id,mt5_ticket,"
            "direction,entry_low,entry_high,entry_price,lot_size,remaining_lots,"
            "stop_loss,status,open_time,strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, signal_id, ticket, "BUY", 4000.0, 4002.0, entry, 0.1, 0.1,
             3990.0, "open", time.time(), "scalp"))


def _resting(trade_id, signal_id=None, status="working", signal_status="pending",
             ticket=55501, age_s=60.0):
    """A resting order, with the signal row its placement path leaves behind.
    Priced 3995 against a 4010 target and a 3985 stop, so no R:R gate refuses
    it on a re-arm and a test cannot pass for that reason."""
    signal_id = signal_id or f"sig-{trade_id}"
    _signal(signal_id, status=signal_status)
    with db.db() as conn:
        conn.execute(
            """INSERT INTO vantage_pending_orders
               (trade_id,signal_id,tg_message_id,channel_name,direction,price,stop_loss,
                tps_json,pcts_json,be_at_pos,tp_open,lot_size,ea_ticket,status,created_at,
                strategy)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, signal_id, None, "Reversal Engine", "BUY", 3995.0, 3985.0,
             json.dumps({"1": 4010.0}), json.dumps([1.0]), 0, 0, 0.1, ticket,
             status, time.time() - age_s, "limit_runner"),
        )


def _order_status(trade_id):
    with db.db() as conn:
        return conn.execute("SELECT status FROM vantage_pending_orders WHERE trade_id=?",
                            (trade_id,)).fetchone()[0]


class _FakeEA:
    """Records the two calls a resting order can receive. No open, no close."""

    def __init__(self):
        self.cancelled: list[tuple] = []
        self.placed: list[str] = []

    async def cancel_pending_order(self, trade_id, ticket, reason):
        self.cancelled.append((trade_id, ticket, reason))
        return True

    async def place_pending_order(self, trade_id, direction, price, lot_size, stop_loss,
                                  tps, pcts, be_at_pos, strategy, expire_minutes=240.0,
                                  close_full_on_last=True, trail_mode=None, template=None):
        self.placed.append(trade_id)
        return {"type": "pending_order_placed", "ticket": 77700 + len(self.placed)}


@pytest.fixture(autouse=True)
def _fresh_guard_state(monkeypatch):
    """The guard's re-arm throttle and over-cap memory are module state; each
    test starts as a freshly booted process would."""
    monkeypatch.setattr(rr, "_last_rearm_at", 0.0)
    monkeypatch.setattr(rr, "_last_over_announced", None)


@pytest.fixture
def quiet_alerts(monkeypatch):
    """Telegram sends recorded, not made."""
    sent: list[tuple] = []

    async def _send(text, trade_id=None, event_type=None, *a, **kw):
        sent.append((event_type, text))
        return True

    from backend.src.services.telegram import alerts
    monkeypatch.setattr(alerts, "send_message", _send)
    return sent


# Both revalidation toggles off: no bias, schedule, news, R:R or momentum
# gate can withdraw or hold anything, so the cap is the only thing deciding.
GATES_OFF = {"htf_bias_gate_enabled": 0, "resting_revalidation_enabled": 0}


def _enforce(ea, rs=None):
    return asyncio.run(rr.enforce_max_open_trades(ea, rs if rs is not None else GATES_OFF))


class TestTheCount:
    """`count_trade_slots_used` is the number every cap gate compares."""

    def test_a_resting_order_holds_no_slot(self, fresh_db):
        """The change."""
        _resting("p-1")

        assert ssr.count_trade_slots_used() == 0

    def test_nor_does_a_withdrawn_one(self, fresh_db):
        _resting("p-1", status="withdrawn")

        assert ssr.count_trade_slots_used() == 0

    def test_an_open_position_holds_one(self, fresh_db):
        _open_trade("t-1")

        assert ssr.count_trade_slots_used() == 1

    def test_an_unmatched_placeholder_still_holds_one(self, fresh_db):
        """Ticket 0, entry 0: an EA Template market order the app has not yet
        matched to its fill. It may be filling now; not counting it is how
        two trades pass a cap with one slot."""
        _open_trade("t-ph", ticket=0, entry=0.0)

        assert ssr.count_trade_slots_used() == 1

    def test_an_open_in_flight_holds_one(self, fresh_db):
        """Unchanged: this is what stops two simultaneous market orders both
        passing a cap of one."""
        _signal("sig-1", status="activating")

        assert ssr.count_trade_slots_used() == 1

    def test_a_claim_whose_order_is_resting_holds_none(self, fresh_db):
        """The Reversal Engine leaves its signal 'activating' while its order
        rests. That order is a resting order; the claim is not an open in
        flight."""
        _resting("p-1", signal_status="activating")

        assert ssr.count_trade_slots_used() == 0

    def test_nor_once_that_order_is_withdrawn(self, fresh_db):
        """The trap in the old guard: it looked only for a 'working' order, so
        a withdrawn one's 'activating' signal counted as an open in flight --
        a withdrawn order holding a slot, the opposite of this rule."""
        _resting("p-1", status="withdrawn", signal_status="activating")

        assert ssr.count_trade_slots_used() == 0

    def test_three_live_and_three_resting_is_three(self, fresh_db):
        for i in range(3):
            _open_trade(f"t-{i}")
            _resting(f"p-{i}")

        assert ssr.count_trade_slots_used() == 3

    def test_the_explanation_says_resting_orders_do_not_count(self, fresh_db):
        _open_trade("t-1")
        _resting("p-1")
        _resting("p-2")

        text = ssr.describe_trade_slots()

        assert "1 open" in text
        assert "2 resting" in text and "not counted" in text


class TestWhatIsAtStake:
    """The other question the old count answered: is anything at the broker
    at all? The EA install at startup and the stale-EA reload both defer
    while it is, because a restart blinds management. A resting order is
    something at stake even though it holds no slot."""

    def test_a_resting_order_is_at_stake(self, fresh_db):
        _resting("p-1")

        assert ssr.count_book_at_stake() == 1

    def test_everything_adds_up(self, fresh_db):
        _open_trade("t-1")
        _resting("p-1")
        _signal("sig-flight", status="activating")

        assert ssr.count_book_at_stake() == 3

    def test_an_empty_book_is_zero(self, fresh_db):
        assert ssr.count_book_at_stake() == 0

    def test_the_startup_install_asks_this_question(self):
        from backend.src import app
        src = inspect.getsource(app)
        assert "count_book_at_stake" in src

    def test_so_does_the_stale_ea_reload(self):
        from backend.src.services.positions import core_ea_link_watchdog as wd
        assert "count_book_at_stake" in inspect.getsource(wd._slots_in_use)


class TestTheClaim:

    def test_resting_orders_alone_never_fill_the_cap(self, fresh_db):
        _set_cap(3)
        for i in range(3):
            _resting(f"p-{i}")
        _signal("sig-new")

        assert ssr.claim_signal_activation("sig-new") == 1

    def test_three_live_trades_do(self, fresh_db):
        _set_cap(3)
        for i in range(3):
            _open_trade(f"t-{i}")
        _signal("sig-new")

        assert ssr.claim_signal_activation("sig-new") == 0

    def test_two_live_and_resting_orders_leave_a_slot(self, fresh_db):
        _set_cap(3)
        _open_trade("t-1")
        _open_trade("t-2")
        _resting("p-1")
        _resting("p-2")
        _signal("sig-new")

        assert ssr.claim_signal_activation("sig-new") == 1

    def test_the_reversal_engines_claim_follows_the_same_count(self, fresh_db):
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db
        _set_cap(2)
        _open_trade("t-1")
        _resting("p-1")
        _signal("sig-new")

        assert re_db.claim_vantage_signal_activation("sig-new") == 1

    def test_and_still_refuses_at_the_cap(self, fresh_db):
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db
        _set_cap(2)
        _open_trade("t-1")
        _open_trade("t-2")
        _signal("sig-new")

        assert re_db.claim_vantage_signal_activation("sig-new") == 0


class TestTheMarketOrderBackstop:
    """`open_trade`'s own check. The bridge has no tick, so an order that gets
    PAST the cap stops at "No live price available" -- before any order call,
    which this bridge does not have."""

    class _NoTickBridge:
        async def get_fresh_tick(self):
            return None

    def _open(self):
        from backend.src.services.trading import open_trade as ot
        return asyncio.run(ot.open_trade(
            self._NoTickBridge(), signal_id="sig-new", direction="BUY",
            entry_low=4000.0, entry_high=4002.0, stop_loss=3990.0,
            tp1=4010.0, lot_size=0.1, strategy="scale_out"))

    def test_resting_orders_do_not_block_a_market_order(self, fresh_db):
        _set_cap(2)
        _resting("p-1")
        _resting("p-2")
        _signal("sig-new")

        with pytest.raises(RuntimeError, match="No live price"):
            self._open()

    def test_live_trades_at_the_cap_do(self, fresh_db):
        _set_cap(2)
        _open_trade("t-1")
        _open_trade("t-2")
        _signal("sig-new")

        with pytest.raises(ValueError, match="Max open trades"):
            self._open()


class TestThePreChecksCountOpensInFlight:
    """The scan, IME and queued-signal pre-checks hold their own open-trades
    list and add what it cannot see. That used to include resting orders."""

    def test_in_flight_only(self, fresh_db):
        _resting("p-1")
        _resting("p-2", status="withdrawn", signal_status="activating")
        _signal("sig-flight", status="activating")

        assert ssr.count_opens_in_flight() == 1

    @pytest.mark.parametrize("module", [
        "backend.src.services.signals.pending_activation",
        "backend.src.services.trading.instant_entry",
        "backend.src.services.trading.scan_auto_execute",
    ])
    def test_each_pre_check_uses_it(self, module):
        import importlib
        src = inspect.getsource(importlib.import_module(module))
        assert "count_opens_in_flight" in src
        assert "count_slots_not_yet_open" not in src


class TestTheGuardAtTheCap:
    """While the book is full, no resting order may stay on it."""

    def test_every_resting_order_is_withdrawn_at_the_cap(self, fresh_db, quiet_alerts):
        _set_cap(3)
        for i in range(3):
            _open_trade(f"t-{i}")
        _resting("p-1", ticket=501)
        _resting("p-2", ticket=502)
        ea = _FakeEA()

        assert _enforce(ea) == 2

        assert sorted(c[1] for c in ea.cancelled) == [501, 502]
        assert _order_status("p-1") == "withdrawn"
        assert _order_status("p-2") == "withdrawn"

    def test_whatever_distance_they_are_from_price(self, fresh_db, quiet_alerts):
        """No proximity rule: the cap is not a condition that may clear
        before price arrives, and a fill is what it exists to prevent."""
        _set_cap(1)
        _open_trade("t-1")
        _resting("p-far", ticket=900)
        ea = _FakeEA()

        _enforce(ea, rs={"htf_bias_gate_enabled": 1, "resting_revalidation_enabled": 1})

        assert [c[1] for c in ea.cancelled] == [900]

    def test_the_reason_names_the_cap(self, fresh_db, quiet_alerts):
        _set_cap(1)
        _open_trade("t-1")
        _resting("p-1")
        ea = _FakeEA()

        _enforce(ea)

        assert "Max open trades" in ea.cancelled[0][2]

    def test_an_open_in_flight_counts_toward_the_cap(self, fresh_db, quiet_alerts):
        """A market order being sent now is about to be live."""
        _set_cap(2)
        _open_trade("t-1")
        _signal("sig-flight", status="activating")
        _resting("p-1")
        ea = _FakeEA()

        _enforce(ea)

        assert len(ea.cancelled) == 1

    def test_below_the_cap_nothing_is_withdrawn(self, fresh_db, quiet_alerts):
        """Control. Without it the file passes against a guard that withdraws
        everything always, which would stop every limit order ever filling."""
        _set_cap(3)
        _open_trade("t-1")
        _open_trade("t-2")
        _resting("p-1")
        _resting("p-2")
        ea = _FakeEA()

        assert _enforce(ea) == 0
        assert ea.cancelled == []
        assert _order_status("p-1") == "working"

    def test_an_unreadable_count_withdraws_nothing(self, fresh_db, monkeypatch, quiet_alerts):
        """Unknown is not "full". Pulling every order on a failed read would
        be a self-inflicted outage -- the same rule the revalidation sweep
        keeps."""
        _set_cap(1)
        _open_trade("t-1")
        _resting("p-1")

        def _boom(*a, **kw):
            raise RuntimeError("database locked")
        monkeypatch.setattr(ssr, "count_trade_slots_used", _boom)
        ea = _FakeEA()

        assert _enforce(ea) == 0
        assert ea.cancelled == []


class TestReArming:

    def test_a_withdrawn_order_stays_off_while_the_book_is_full(self, fresh_db, quiet_alerts):
        _set_cap(1)
        _open_trade("t-1")
        _resting("p-1", status="withdrawn")
        ea = _FakeEA()

        _enforce(ea)

        assert ea.placed == []
        assert _order_status("p-1") == "withdrawn"

    def test_it_comes_back_when_a_slot_frees(self, fresh_db, quiet_alerts):
        """With both revalidation toggles off the 60s sweep never runs, so
        this is the only thing that can put it back."""
        _set_cap(2)
        _open_trade("t-1")
        _resting("p-1", status="withdrawn")
        ea = _FakeEA()

        _enforce(ea)

        assert ea.placed == ["p-1"]
        assert _order_status("p-1") == "working"

    def test_the_sweep_holds_it_off_while_the_book_is_full(self, fresh_db, quiet_alerts):
        """With the toggles on, re-arming belongs to the 60s sweep, and every
        gate it asks must now include the cap -- or the sweep puts back what
        the guard has just taken off, every minute."""
        _set_cap(1)
        _open_trade("t-1")
        _resting("p-1", status="withdrawn")
        ea = _FakeEA()

        asyncio.run(rr.revalidate_resting_orders(
            ea, {"htf_bias_gate_enabled": 0, "resting_revalidation_enabled": 1},
            bias=None, tick=None, dpm_candles=None))

        assert ea.placed == []

    def test_the_sweep_withdraws_at_the_cap_too(self, fresh_db, quiet_alerts):
        """A backstop, if the per-cycle guard ever misses a cycle."""
        _set_cap(1)
        _open_trade("t-1")
        _resting("p-1", ticket=321)
        ea = _FakeEA()

        asyncio.run(rr.revalidate_resting_orders(
            ea, {"htf_bias_gate_enabled": 0, "resting_revalidation_enabled": 1},
            bias=None, tick=None, dpm_candles=None))

        assert [c[1] for c in ea.cancelled] == [321]

    def test_the_re_arm_is_not_retried_every_cycle(self, fresh_db, quiet_alerts):
        """The per-cycle guard runs every 1-5s. A re-arm the broker refuses
        ("Invalid price", price already through the level) must not be sent
        to the EA every few seconds."""
        _set_cap(2)
        _open_trade("t-1")
        _resting("p-1", status="withdrawn")

        class _Refusing(_FakeEA):
            async def place_pending_order(self, trade_id, *a, **kw):
                self.placed.append(trade_id)
                return {"type": "pending_order_open_failed", "error": "Invalid price"}

        ea = _Refusing()
        _enforce(ea)
        _enforce(ea)

        assert ea.placed == ["p-1"]


class TestOverTheCap:
    """The residual risk, stated rather than hidden: a resting order can fill
    in the second between the book filling and the guard's next cycle. The
    guard cannot undo that -- closing a position is not its job -- but the
    owner must hear about it."""

    def test_it_is_announced_once(self, fresh_db, quiet_alerts):
        _set_cap(1)
        _open_trade("t-1")
        _open_trade("t-2")
        ea = _FakeEA()

        _enforce(ea)
        _enforce(ea)

        over = [s for s in quiet_alerts if s[0] == "max_open_trades_exceeded"]
        assert len(over) == 1
        assert "2" in over[0][1] and "1" in over[0][1]


class TestItNeverCloses:

    def test_the_guard_names_no_close_path(self):
        src = inspect.getsource(rr.enforce_max_open_trades)
        for name in ("close_trade", "record_close", "partial_close", "close_position"):
            assert name not in src


class TestTheMonitorCycleRunsIt:

    def test_every_cycle_not_every_minute(self):
        """The 60s sweep is too slow for this: a resting order can fill in a
        minute. The guard runs on the cycle itself, outside the sweep's 60s
        timer."""
        from backend.src.services.positions import monitor_cycle as mc
        src = inspect.getsource(mc.run_monitor_cycle)
        guard_at = src.index("enforce_max_open_trades")
        timer_at = src.index("last_resting_sweep > 60.0")
        assert guard_at < timer_at


# ── Carried over from test_resting_orders_consume_a_slot.py ──────────────────
# That file pinned the 2026-09-04 rule this one reverses, and was removed with
# it (owner-approved, 2026-09-28). What it pinned that is still true is below,
# unchanged except where a resting order used to count.

class TestAFilledOrPulledOrder:

    def test_a_filled_order_is_counted_once_as_the_position_it_became(self, fresh_db):
        _resting("p-1", status="filled")
        _open_trade("p-1-pos", signal_id="sig-p-1-pos")

        assert ssr.count_trade_slots_used() == 1

    def test_a_cancelled_order_counts_for_nothing(self, fresh_db):
        _resting("p-1", status="cancelled")

        assert ssr.count_trade_slots_used() == 0
        assert ssr.count_book_at_stake() == 0


class TestASignalDoesNotBlockItself:
    """The claim comes FIRST and `open_trade` runs second, for the same
    signal. A backstop that counts in-flight claims without excluding the
    caller's own refuses every trade the normal path ever tries."""

    def test_a_claim_counts_against_everyone_else(self, fresh_db):
        _signal("sig-1", status="activating")

        assert ssr.count_trade_slots_used() == 1

    def test_but_not_against_the_signal_it_belongs_to(self, fresh_db):
        _signal("sig-1", status="activating")

        assert ssr.count_trade_slots_used(exclude_signal_id="sig-1") == 0

    def test_an_open_row_on_the_same_signal_still_counts(self, fresh_db):
        """Only the in-flight half is excluded. A position that exists is a
        slot however it got there."""
        _open_trade("t-1", signal_id="sig-1")

        assert ssr.count_trade_slots_used(exclude_signal_id="sig-1") == 1

    def test_other_signals_are_untouched_by_the_exclusion(self, fresh_db):
        """Was 2 under the old rule: the resting p-9 no longer counts."""
        _signal("sig-1", status="activating")
        _signal("sig-2", status="activating")
        _resting("p-9")

        assert ssr.count_trade_slots_used(exclude_signal_id="sig-1") == 1


class TestARefusedClaim:

    def test_leaves_the_signal_alone(self, fresh_db):
        """A refused claim must not consume the signal -- the scheduler
        retries it once a slot frees."""
        _set_cap(1)
        _open_trade("t-1")
        _signal("sig-new")

        assert ssr.claim_signal_activation("sig-new") == 0

        with db.db() as conn:
            status = conn.execute("SELECT status FROM vantage_signals WHERE signal_id=?",
                                  ("sig-new",)).fetchone()[0]
        assert status == "pending"

    def test_says_where_the_slots_went(self, fresh_db):
        _set_cap(2)
        _open_trade("t-1")
        _open_trade("t-2")
        _signal("sig-new")

        assert "2 open" in ssr.explain_failed_claim("sig-new")

    def test_the_reversal_engines_granted_claim_still_stamps_activated_at(self, fresh_db):
        """release_stranded_activations releases any 'activating' row whose
        activated_at is NULL. The cap must not cost the stamp."""
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db
        _set_cap(2)
        _signal("sig-new")

        re_db.claim_vantage_signal_activation("sig-new")

        with db.db() as conn:
            stamped = conn.execute(
                "SELECT activated_at FROM vantage_signals WHERE signal_id=?",
                ("sig-new",)).fetchone()[0]
        assert stamped is not None
