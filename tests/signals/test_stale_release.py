"""Guards for the moment a backlog of queued signals is released at once.

**No test in this file places, closes or modifies any MT5 order.** The three
watcher tests mock `open_trade_from_signal` with an `AsyncMock`; the rest call
pure functions that take a tick dataclass and return a string or None. Nothing
here holds a bridge.

WHAT HAPPENED (2026-09-21, owner report)
----------------------------------------
Trading had been halted by the daily-loss limit since 13:21. At 14:22:41 the
owner lifted it, and inside 700ms the pending watcher opened three positions:

    14:22:41.483  [GapRevalidation] away 3745s — 5 queued signals must re-validate
    14:22:41.754  [EA] order placed BUY  0.1 @ 4362.33   (signal age 3149s)
    14:22:41.953  [EA] order placed SELL 0.1 @ 4361.82   (signal age 2695s)
    14:22:42.152  [EA] order placed BUY  0.1 @ 4361.85   (signal age 1388s)

Two buys and a sell, on one instrument, within half a point of each other. The
re-validation in `gap_revalidation.py` DID run -- it is in the log -- and every
gate it suspends the bypass for passed. Three separate holes let the rest
through, and this file covers all three.

**1. The momentum check has never run on this account.** The watcher defers a
signal whose direction disagrees with the last M5 candle
(`pending_activation.py`, "momentum mismatch") -- the one gate that makes a BUY
and a SELL mutually exclusive on the same tick. It reads `dpm_candles`, which
`monitor_cycle` only fills when `dpm_enabled`. That setting is 0 here, so the
list is permanently empty and the gate is dead code.

**2. The zone test rationalises both directions at the same price.**
`governor.price_in_entry_range` allows an unbounded "better than zone" fill: a
SELL fills at any bid at or above `entry_low`. The SELL's zone topped out at
4354.65 and it sold at 4361.82 -- seven points above its own zone, scored as a
*better* entry. Meanwhile IME gap-fire dragged both BUY zones up to 4362.38 as
an acceptable chase. Note the asymmetry that made this possible: the chase side
is capped by `scan_auto_execute.MAX_GAP_FIRE_PTS`, the better-fill side by
nothing at all.

**3. Nothing compares the released signals to each other.**
`signals/contradiction.py` is shadow-only by design and is not wired to
execution; `positions/core_internal_exposure_guard.py` is called only from the
Reversal and Breakout live-execute paths and excludes Telegram trades outright.
The watcher asks nothing.

All three guards are OFF by default (owner, 2026-09-21, and
rules/60-adding-a-tunable): nothing trades differently until a dial moves. The
"default is off" tests below are what pin that, and they are as load-bearing as
the ones that prove the guards bite.
"""
from __future__ import annotations

import asyncio
import time
import types
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.signals import pending_activation as psa
from backend.src.services.signals import stale_release as sr


# A tick sitting well above a 2380-2385 sell zone and well below a
# 2415-2420 buy zone: the same price is "better than the zone" for one and
# "better than the zone" for the other, which is the shape of the incident.
_TICK = types.SimpleNamespace(bid=2400.0, ask=2400.5)


# ── 1. How far past its own zone a fill actually is ──────────────────────────

class TestBetterFillExcess:
    """Only the favourable side. The unfavourable side is already answered --
    `price_in_entry_range` refuses it outright, and IME gap-fire is capped by
    MAX_GAP_FIRE_PTS."""

    def test_a_sell_filling_above_its_zone_reports_the_distance_above(self):
        assert sr.better_fill_excess_pts("SELL", 2380.0, 2385.0, _TICK) == 15.0

    def test_a_buy_filling_below_its_zone_reports_the_distance_below(self):
        assert sr.better_fill_excess_pts("BUY", 2415.0, 2420.0, _TICK) == 14.5

    def test_a_fill_inside_the_zone_is_no_excess_at_all(self):
        assert sr.better_fill_excess_pts("SELL", 2395.0, 2405.0, _TICK) == 0.0

    def test_a_fill_on_the_unfavourable_side_is_not_reported_as_excess(self):
        """A SELL below its zone is price running away, not a gift. It is
        `price_in_entry_range`'s refusal to make, and a negative number here
        would read as a cap that can never trip."""
        assert sr.better_fill_excess_pts("SELL", 2420.0, 2425.0, _TICK) == 0.0


# ── 2. The cap built on that distance ────────────────────────────────────────

def _cap_rs(**kw) -> dict:
    return {"stale_better_fill_cap_enabled": 1,
            "stale_better_fill_cap_pts": 10.0, **kw}


class TestBetterFillCap:

    def test_it_is_off_by_default(self):
        """An empty settings dict is what an un-migrated row and every
        existing test harness look like. The cap must be inert for both."""
        assert sr.better_fill_blocked({}, "SELL", 2380.0, 2385.0, _TICK) is None

    def test_a_fill_further_past_the_zone_than_the_cap_is_refused(self):
        assert sr.better_fill_blocked(
            _cap_rs(), "SELL", 2380.0, 2385.0, _TICK) is not None

    def test_the_refusal_names_the_distance_and_the_cap(self):
        """The log line is the only place the owner sees why a queued signal
        did not fire. "held -- blocked" tells them nothing."""
        why = sr.better_fill_blocked(_cap_rs(), "SELL", 2380.0, 2385.0, _TICK)

        assert "15" in why and "10" in why

    def test_a_fill_inside_the_zone_is_allowed_with_the_cap_on(self):
        assert sr.better_fill_blocked(
            _cap_rs(), "SELL", 2395.0, 2405.0, _TICK) is None

    def test_a_fill_exactly_at_the_cap_is_allowed(self):
        """Boundary, matched deliberately to `0 < gap <= MAX_GAP_FIRE_PTS` on
        the chase side. Two caps on the same quantity that disagree about
        their own edge is how the two paths drift."""
        assert sr.better_fill_blocked(
            _cap_rs(stale_better_fill_cap_pts=15.0),
            "SELL", 2380.0, 2385.0, _TICK) is None

    def test_a_fill_a_hair_over_the_cap_is_refused(self):
        assert sr.better_fill_blocked(
            _cap_rs(stale_better_fill_cap_pts=14.99),
            "SELL", 2380.0, 2385.0, _TICK) is not None

    def test_an_unreadable_cap_falls_back_to_the_shipped_default(self):
        """A settings row can hold anything. Raising here would abort the
        whole release pass, which is a worse failure than the one the cap
        exists to prevent."""
        assert sr.better_fill_blocked(
            _cap_rs(stale_better_fill_cap_pts="not a number"),
            "SELL", 2380.0, 2385.0, _TICK) is None

    def test_the_default_cap_matches_the_chase_side(self):
        """One quantity, one number. If MAX_GAP_FIRE_PTS moves and this does
        not, the app will chase 15 points up and accept 20 down."""
        from backend.src.services.trading.scan_auto_execute import MAX_GAP_FIRE_PTS

        assert sr.BETTER_FILL_CAP_PTS_DEFAULT == MAX_GAP_FIRE_PTS


# ── 3. Opposing directions inside one release ────────────────────────────────

class TestBurstHedgeGuard:

    def test_it_is_off_by_default(self):
        assert sr.burst_hedge_blocked({}, "SELL", ["BUY"]) is None

    def test_the_first_signal_of_a_pass_is_never_blocked(self):
        assert sr.burst_hedge_blocked(
            {"burst_hedge_guard_enabled": 1}, "BUY", []) is None

    def test_a_second_signal_on_the_same_side_is_allowed(self):
        """This is a hedge guard, not a position-count cap -- `max_open_trades`
        already owns that question, and two buys released together are one
        view expressed twice."""
        assert sr.burst_hedge_blocked(
            {"burst_hedge_guard_enabled": 1}, "BUY", ["BUY"]) is None

    def test_a_signal_opposing_one_already_released_this_pass_is_refused(self):
        assert sr.burst_hedge_blocked(
            {"burst_hedge_guard_enabled": 1}, "SELL", ["BUY"]) is not None

    def test_direction_case_does_not_decide_the_answer(self):
        """Signal rows carry both cases; `sig["direction"].upper()` is applied
        at some call sites and not others."""
        assert sr.burst_hedge_blocked(
            {"burst_hedge_guard_enabled": 1}, "sell", ["buy"]) is not None


# ── 4. The momentum gate's own switch ────────────────────────────────────────

class TestMomentumGateSwitch:

    def test_it_is_off_by_default(self):
        assert sr.momentum_gate_enabled({}) is False

    def test_it_reads_its_own_setting(self):
        assert sr.momentum_gate_enabled(
            {"pending_momentum_gate_enabled": 1}) is True

    def test_it_is_not_the_dpm_setting(self):
        """The check has always been reachable with `dpm_enabled` on. This
        toggle exists to reach it WITHOUT turning DPM on, so reading that
        setting here would make the new switch a no-op for the one
        configuration it was added for."""
        assert sr.momentum_gate_enabled({"dpm_enabled": 1}) is False


# ── 5. The watcher, end to end ───────────────────────────────────────────────

def _insert(sig_id, direction, entry_low, entry_high, stop_loss, tp1):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, source_name, direction, "
            "entry_low, entry_high, stop_loss, tp1, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (sig_id, "Telegram Auto (Gold Diggers Scalping)", direction,
             entry_low, entry_high, stop_loss, tp1, "pending", time.time()),
        )


def _run_watcher(rs):
    """One release pass with the order call replaced by a recorder."""
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock(return_value={"entry_price": 2400.0})) as ot:
        asyncio.run(psa.try_activate_pending_signals(_TICK, rs, object(), {}, []))
    return ot


def _base_rs(**kw) -> dict:
    return {"max_open_trades": 3, "trade_strategy": "scale_out", **kw}


# A sell whose zone price has already run 15 points past, on the favourable
# side. Stop above the live price and a distant TP1, so the R:R filter has no
# opinion and only the guard under test can refuse it.
_OVEREXTENDED_SELL = ("sig-sell", "SELL", 2380.0, 2385.0, 2405.0, 2360.0)
# A buy in its zone at the same tick.
_BUY_IN_ZONE = ("sig-buy", "BUY", 2398.0, 2402.0, 2390.0, 2420.0)


class TestTheWatcherHonoursTheCap:

    def test_by_default_the_overextended_sell_still_fills(self, fresh_db):
        """The characterization half. This is what happened on 2026-09-21 and
        it must keep happening until the owner turns the cap on -- a release
        that silently starts refusing trades is the failure that
        rules/60-adding-a-tunable exists to prevent."""
        _insert(*_OVEREXTENDED_SELL)

        ot = _run_watcher(_base_rs())

        assert ot.await_count == 1

    def test_with_the_cap_on_the_overextended_sell_is_held(self, fresh_db):
        _insert(*_OVEREXTENDED_SELL)

        ot = _run_watcher(_base_rs(stale_better_fill_cap_enabled=1,
                                   stale_better_fill_cap_pts=10.0))

        assert ot.await_count == 0

    def test_the_held_signal_stays_queued_rather_than_being_expired(self, fresh_db):
        """Held, not killed. Price may come back to the zone inside the
        signal's own window, and that fill is the one it was written for."""
        _insert(*_OVEREXTENDED_SELL)

        _run_watcher(_base_rs(stale_better_fill_cap_enabled=1,
                              stale_better_fill_cap_pts=10.0))

        with db.db() as conn:
            status = conn.execute(
                "SELECT status FROM vantage_signals WHERE signal_id='sig-sell'"
            ).fetchone()[0]
        assert status == "pending"

    def test_a_fill_inside_the_cap_is_untouched_by_it(self, fresh_db):
        _insert(*_OVEREXTENDED_SELL)

        ot = _run_watcher(_base_rs(stale_better_fill_cap_enabled=1,
                                   stale_better_fill_cap_pts=20.0))

        assert ot.await_count == 1


class TestTheWatcherHonoursTheBurstGuard:

    def test_by_default_both_sides_of_the_backlog_are_released(self, fresh_db):
        """2026-09-21 again, reduced to two signals."""
        _insert(*_BUY_IN_ZONE)
        _insert(*_OVEREXTENDED_SELL)

        ot = _run_watcher(_base_rs())

        assert ot.await_count == 2

    def test_with_the_guard_on_only_one_direction_is_released(self, fresh_db):
        _insert(*_BUY_IN_ZONE)
        _insert(*_OVEREXTENDED_SELL)

        ot = _run_watcher(_base_rs(burst_hedge_guard_enabled=1))

        assert ot.await_count == 1

    def test_the_guard_does_not_block_two_signals_on_the_same_side(self, fresh_db):
        """The cap it must not become. Both are buys; `max_open_trades` is 3."""
        _insert("sig-buy-a", "BUY", 2398.0, 2402.0, 2390.0, 2420.0)
        _insert("sig-buy-b", "BUY", 2397.0, 2401.0, 2389.0, 2419.0)

        ot = _run_watcher(_base_rs(burst_hedge_guard_enabled=1))

        assert ot.await_count == 2
