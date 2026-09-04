"""One close, one outcome — the loser of the race books nothing.

Companion to tests/trading/test_close_alert_not_sent_twice.py, which stopped
the duplicate Telegram message. The alert was the visible half. This is the
half nobody would have seen.

`apply_full_close` is a compare-and-set, so the trade row and the account
balance are already safe from a second caller. Everything AFTER it in
`record_close` is not, and three of those blocks record an outcome rather than
re-evaluate one:

  * `record_live_trade_outcome` increments `circuit_breaker_consec_losses`.
    One losing trade closed by two racing callers is counted as two losses.
    With the threshold at 3, two real losses trip a breaker that blocks live
    execution for the cooldown -- the app halts itself over a loss that never
    happened. The duplicate cannot be spotted afterwards either: tripping
    RESETS the counter, so the evidence deletes itself.
  * `push_trade_closed` upserts the consolidated ledger keyed on
    (node_id, trade_id). The second push carries the loser's numbers, and the
    loser's `gross_pnl` is 0 because the winner already zeroed remaining_lots
    -- so `outcome` is computed as "be" and OVERWRITES the real win or loss.
    Every win-rate the Edge Dashboard shows is read from that column.
  * `finalize_dpm_record` rewrites the DPM learning record, including a
    hold time measured to the wrong moment.

The protective halts are deliberately NOT in that list. `rg_apply_halts_on_close`,
the give-back guard and the daily-loss ceiling read the current balance and
decide whether to stop trading; running one twice re-reaches the same verdict,
and skipping a protective check to tidy up a duplicate is the wrong trade in
the wrong direction. They still run for both callers, and the tests at the
bottom pin that as a decision rather than an oversight.

No order is placed, closed or modified anywhere here: the bridge is a fake that
answers an account balance and nothing else.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from backend.src.db import database as db
from backend.src.services.trading import close_trade as ct


class _Bridge:
    """Answers a balance. It has no close_position at all -- record_close
    never places or closes anything, and this fake makes that structural."""

    async def get_account(self):
        return {"balance": 1000.0}

    async def get_tick(self):
        return SimpleNamespace(bid=4437.90, ask=4438.10)


@pytest.fixture
def ctx(fresh_db):
    return ct.CloseTradeContext(_Bridge(), starting_balance=1000.0)


@pytest.fixture
def cb_armed(fresh_db):
    """Circuit breaker on, threshold 3, nothing counted yet."""
    db.update_risk_settings({
        "circuit_breaker_enabled": 1,
        "circuit_breaker_losses": 3,
        "circuit_breaker_cooldown_mins": 60,
        "circuit_breaker_consec_losses": 0,
    })


def _insert(trade_id="t-1", *, mt5_ticket=1940612275, net_pnl=0.0,
            direction="BUY", entry=2400.0):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("s-" + trade_id, direction, 2399.0, 2401.0, 2390.0, "active", time.time()))
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, "
            "direction, entry_low, entry_high, entry_price, lot_size, remaining_lots, "
            "stop_loss, status, open_time, net_pnl, realised_pnl, strategy) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, "s-" + trade_id, mt5_ticket, direction, 2399.0, 2401.0, entry,
             0.10, 0.10, 2390.0, "open", time.time(), net_pnl, 0.0, "scale_out"))


def _consec() -> int:
    return int(db.get_risk_settings().get("circuit_breaker_consec_losses", 0) or 0)


def _close(ctx, trade_id="t-1", price=2380.0, reason="SL"):
    """A BUY closed below entry -- a loss."""
    return asyncio.run(ct.record_close(trade_id, price, reason, ctx))


def _ledger_outcome(trade_id="t-1"):
    with db.db() as conn:
        row = conn.execute(
            "SELECT outcome FROM consolidated_trades WHERE trade_id=?",
            (trade_id,)).fetchone()
    return row[0] if row else None


# ── the circuit breaker ───────────────────────────────────────────────────────

class TestOneLossCountsOnce:

    def test_the_first_close_counts_it(self, cb_armed, ctx):
        """The negative control. Without it every test below would pass
        against a guard that stopped the breaker counting anything at all --
        which is the one failure mode worse than counting twice."""
        _insert()

        _close(ctx)

        assert _consec() == 1

    def test_a_SECOND_caller_does_not_count_it_AGAIN(self, cb_armed, ctx):
        _insert()

        _close(ctx)
        _close(ctx)

        assert _consec() == 1, "one losing trade was counted as two"

    def test_a_duplicate_close_CANNOT_TRIP_THE_BREAKER(self, cb_armed, ctx):
        """The money consequence, and the reason the counter alone is not
        enough to assert on: tripping resets consec_losses to 0, so a phantom
        trip looks identical to a clean slate. With the threshold at 2, one
        real loss closed twice was enough to block live execution for the
        whole cooldown."""
        db.update_risk_settings({"circuit_breaker_losses": 2})
        _insert()

        _close(ctx)
        _close(ctx)

        assert db.get_circuit_breaker_state()["is_active"] is False, (
            "the breaker tripped on one loss counted twice — live execution "
            "is now blocked for the cooldown over a trade that never happened")

    def test_a_win_still_resets_the_counter(self, cb_armed, ctx):
        """The other half of the control: the guard must not stop the breaker
        seeing a win either."""
        db.update_risk_settings({"circuit_breaker_consec_losses": 2})
        _insert()

        _close(ctx, price=2420.0)          # BUY closed above entry -> win

        assert _consec() == 0

    def test_a_real_second_LOSS_still_counts(self, cb_armed, ctx):
        """The guard is per-close, not per-session. Two genuinely different
        losing trades are still two losses."""
        _insert("t-1")
        _insert("t-2")

        _close(ctx, "t-1")
        _close(ctx, "t-2")

        assert _consec() == 2


# ── the consolidated ledger ───────────────────────────────────────────────────

class TestTheLedgerKeepsTheRealOutcome:

    def test_the_first_close_records_the_real_outcome(self, fresh_db, ctx):
        _insert()

        _close(ctx)

        assert _ledger_outcome() == "loss"

    def test_a_SECOND_caller_does_not_rewrite_it_to_BE(self, fresh_db, ctx):
        """The upsert is keyed on (node_id, trade_id), so the second push
        lands on the same row. Its gross_pnl is 0 -- the winner already zeroed
        remaining_lots -- and 0 grades as "be", which is what the dashboard's
        win rate then reads."""
        _insert()

        _close(ctx)
        _close(ctx)

        assert _ledger_outcome() == "loss", (
            "the duplicate close overwrote the real outcome with a scratch")

    def test_a_WIN_is_not_rewritten_either(self, fresh_db, ctx):
        _insert()

        _close(ctx, price=2420.0)
        _close(ctx, price=2420.0)

        assert _ledger_outcome() == "win"


# ── the DPM learning record ───────────────────────────────────────────────────

class TestTheDPMRecordIsFinalisedOnce:

    @pytest.fixture
    def dpm_on(self, fresh_db):
        db.update_risk_settings({"dpm_enabled": 1})

    @pytest.fixture
    def finalised(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(ct, "finalize_dpm_record",
                            lambda *a, **k: calls.append(a))
        return calls

    def test_the_first_close_finalises_it(self, dpm_on, ctx, finalised):
        _insert()

        _close(ctx)

        assert len(finalised) == 1

    def test_a_SECOND_caller_does_not_finalise_it_again(self, dpm_on, ctx, finalised):
        _insert()

        _close(ctx)
        _close(ctx)

        assert len(finalised) == 1


# ── what the loser must still do ──────────────────────────────────────────────

class TestTheProtectiveHaltsStillRunForBoth:
    """Deliberate, not an oversight. These read the live balance and decide
    whether to stop trading; a second call reaches the same verdict, and
    skipping a protective check to tidy up a duplicate would be trading a real
    risk for a cosmetic one."""

    @pytest.fixture
    def halt_checks(self, monkeypatch):
        calls: dict = {"giveback": 0, "daily_loss": 0}
        monkeypatch.setattr(ct, "apply_giveback_guard_on_close",
                            lambda *a, **k: calls.__setitem__("giveback", calls["giveback"] + 1))
        monkeypatch.setattr(ct, "apply_daily_loss_halt_on_close",
                            lambda *a, **k: calls.__setitem__("daily_loss", calls["daily_loss"] + 1))
        return calls

    def test_the_give_back_guard_runs_for_the_loser_too(self, fresh_db, ctx, halt_checks):
        _insert()

        _close(ctx)
        _close(ctx)

        assert halt_checks["giveback"] == 2

    def test_the_daily_loss_ceiling_runs_for_the_loser_too(self, fresh_db, ctx, halt_checks):
        _insert()

        _close(ctx)
        _close(ctx)

        assert halt_checks["daily_loss"] == 2
