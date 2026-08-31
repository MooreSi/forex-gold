"""The five stage-3 killer demos, driven offline against the fake bridge.

Each stage-3 task ends with a "killer demo" -- the one scenario that, run on a
broker, proves the fix does what it claims. Those demos still need Simon and a
demo terminal; nothing here substitutes for that, and no task becomes `done`
because this file is green.

What this file IS: the same five scenarios driven end-to-end through the real
pipeline with the broker boundary replaced by FakeMT5Bridge. It proves the code
paths join up under realistic conditions rather than in isolation, and it is
the rehearsal script -- each test names the manual steps its demo maps to, so
the terminal session is a confirmation rather than an exploration.

NO REAL OR DEMO ORDER CAN BE PLACED HERE. The bridge is FakeMT5Bridge (no
network code at all) and the runtime is built with an empty bridge URL.

Demos, and where each is asserted:

    010 order-send dedup      -> test_010_*
    020 timeout means UNKNOWN -> test_020_*
    030 broker<->DB reconcile -> test_030_*
    040 no DB close on a      -> test_040_*
        refused broker close
    050 protective halts      -> test_050_*
"""
from __future__ import annotations

import asyncio

from backend.src.runtime import TradingRuntime
from backend.src.services.broker.ea_bridge import COMMENT_ID_LEN
from backend.src.services.broker.fake_bridge import FakeMT5Bridge
from backend.src.services.positions.monitor_cycle import run_monitor_cycle
from backend.src.services.telegram.fake_reader import FakeTelegramReader

BASE = 1_700_000_000.0

SIGNAL_TEXT = (
    "This is not financial advice. Use appropriate risk management "
    "if you're going to trade.\n"
    "Buy Gold 2399 - 2402\n"
    "Stop Loss 2392\n"
    "TP1 2410  TP2 2418  TP3 2426"
)

CONFIG = {
    "starting_balance": 1000.0, "anthropic_api_key": "",
    "mt5_bridge_url": "", "mt5_native_bridge_enabled": False,
    "telegram_api_id": "", "telegram_api_hash": "",
    "sessions_dir": "./data/test_sessions",
}


def _runtime(clock: dict, anchors: list, signals: list | None = None) -> tuple:
    engine = TradingRuntime(CONFIG)
    engine._bridge = FakeMT5Bridge(
        seed=1, scenario={"anchors": anchors}, base_ts=BASE,
        clock=lambda: clock["now"], starting_balance=1000.0,
    )
    if signals is None:
        signals = [{"at": 0, "channel": "Debug Channel", "text": SIGNAL_TEXT}]
    engine.set_telegram_reader(
        reader := FakeTelegramReader(CONFIG, scenario={"signals": signals})
    )
    return engine, reader


async def _cycles(engine, n: int = 3) -> None:
    for _ in range(n):
        await run_monitor_cycle(engine._make_monitor_ctx())


# ─────────────────────────────────────────────────────────────────────────────
# 040 — a refused broker close must not become a database close
# ─────────────────────────────────────────────────────────────────────────────

def test_040_refused_broker_close_leaves_the_trade_open(fresh_db, caplog):
    """
    Terminal demo this stands in for:
        1. Open one trade on the demo account.
        2. Set the profit-close target low enough that the next tick trips it.
        3. Disable AutoTrading in the MT5 terminal so the close is refused.
        4. Expect: the trade is still open in the app AND in MT5, an alert
           arrives, and NOTHING is written to history.

    The old behaviour wrote a close row at the local tick price whatever the
    broker said, so the app stopped managing a position that was still live.
    """
    clock = {"now": BASE}
    engine, reader = _runtime(clock, [[0, 2400.5], [300, 2408.0]])
    fresh_db.update_risk_settings({
        "auto_execute_signals": 1, "accept_tg_signals": 1,
        "profit_close_usd": 0.01,   # any profit at all trips the target
    })
    reader.feed_due(now=1.0)
    asyncio.run(engine._scan_messages())

    with fresh_db.db() as conn:
        before = conn.execute(
            "SELECT trade_id, status, mt5_ticket FROM vantage_simulated_trades"
        ).fetchone()
    assert before["status"] == "open"

    # The broker refuses every close attempt, the way AutoTrading-off does.
    engine._bridge.inject_error(
        "close_position",
        {"success": False, "error": "AutoTrading disabled by client"},
        count=10,
    )

    clock["now"] = BASE + 300       # in profit; the target trips
    with caplog.at_level("ERROR"):
        asyncio.run(_cycles(engine))

    with fresh_db.db() as conn:
        after = conn.execute(
            "SELECT status, exit_reason, net_pnl FROM vantage_simulated_trades"
        ).fetchone()
        closes = conn.execute(
            "SELECT COUNT(*) c FROM vantage_partial_closes"
        ).fetchone()["c"]

    # The whole point: no close row, no P&L, still managed.
    assert after["status"] == "open"
    assert not after["exit_reason"]
    assert closes == 0

    # And the broker still holds it -- the app and MT5 agree.
    assert [p["ticket"] for p in asyncio.run(engine._bridge.get_positions())] == [
        before["mt5_ticket"]
    ]

    # Loud, not a debug line.
    assert any(
        "NOT closed" in r.message or "NOT closed" in r.getMessage()
        for r in caplog.records if r.levelname == "ERROR"
    ), [r.getMessage() for r in caplog.records]


def test_040_control_a_confirmed_close_still_records(fresh_db):
    """Negative control for the test above: with no injected refusal the same
    scenario closes normally. Without this, a fix that broke ALL closing would
    look identical to a fix that only blocked refused ones."""
    clock = {"now": BASE}
    engine, reader = _runtime(clock, [[0, 2400.5], [300, 2408.0]])
    fresh_db.update_risk_settings({
        "auto_execute_signals": 1, "accept_tg_signals": 1,
        "profit_close_usd": 0.01,
    })
    reader.feed_due(now=1.0)
    asyncio.run(engine._scan_messages())

    clock["now"] = BASE + 300
    asyncio.run(_cycles(engine))

    with fresh_db.db() as conn:
        row = conn.execute(
            "SELECT status, exit_reason FROM vantage_simulated_trades"
        ).fetchone()
    assert row["status"] == "closed"
    assert row["exit_reason"] == "profit_close_target"
    assert asyncio.run(engine._bridge.get_positions()) == []


# ─────────────────────────────────────────────────────────────────────────────
# 050 — trip the breaker, the next signal is refused
# ─────────────────────────────────────────────────────────────────────────────

SECOND_SIGNAL = (
    "Buy Gold 2390 - 2393\n"
    "Stop Loss 2383\n"
    "TP1 2401  TP2 2409  TP3 2417"
)


def test_050_daily_loss_halt_refuses_the_next_signal(fresh_db, caplog):
    """
    Terminal demo this stands in for:
        1. Set the daily loss limit low enough that one losing trade breaches it.
        2. Let a trade close at its stop.
        3. Send another signal.
        4. Expect: it is NOT executed, the app shows the halt reason, and the
           reason names the number that tripped it.

    Before stage3/050 the governor's fallback said 20% for both limits while the
    schema said 3%/8%, so on a default install this breaker could not fire at a
    number anyone had agreed to.
    """
    clock = {"now": BASE}
    engine, reader = _runtime(clock, [[0, 2400.5], [300, 2391.0]], signals=[
        {"at": 0,  "channel": "Debug Channel", "text": SIGNAL_TEXT},
        {"at": 10, "channel": "Debug Channel", "text": SECOND_SIGNAL},
    ])
    fresh_db.update_risk_settings({
        "auto_execute_signals": 1, "accept_tg_signals": 1,
        "max_daily_loss_pct": 0.001,     # any real loss breaches it
    })
    reader.feed_due(now=1.0)
    asyncio.run(engine._scan_messages())

    clock["now"] = BASE + 300            # through the 2392 stop
    asyncio.run(_cycles(engine))

    with fresh_db.db() as conn:
        first = conn.execute(
            "SELECT status, exit_reason, net_pnl FROM vantage_simulated_trades"
        ).fetchone()
    assert first["status"] == "closed" and first["exit_reason"] == "SL"
    assert first["net_pnl"] < 0, "the demo needs a real loss to breach the limit"

    # The breaker is now armed.
    assert engine.is_trading_paused() is True
    with fresh_db.db() as conn:
        reason = conn.execute(
            "SELECT value FROM app_config WHERE key='risk_halt_reason'"
        ).fetchone()
    assert reason and "Daily loss limit hit" in reason["value"]

    # A second signal arrives while halted.
    reader.feed_due(now=10.0)
    new_signals = asyncio.run(engine._scan_messages())

    assert new_signals, "the second signal should still be RECORDED, just not traded"
    assert all(not s.get("auto_executed") for s in new_signals)

    # And no second position exists, at the broker or in the database.
    assert asyncio.run(engine._bridge.get_positions()) == []
    with fresh_db.db() as conn:
        opens = conn.execute(
            "SELECT COUNT(*) c FROM vantage_simulated_trades WHERE status='open'"
        ).fetchone()["c"]
    assert opens == 0


def test_050_control_without_the_breaker_the_second_signal_trades(fresh_db):
    """Negative control: the identical script with the limit left at 0 executes
    the second signal. Without it, a halt that refused EVERYTHING would pass."""
    clock = {"now": BASE}
    engine, reader = _runtime(clock, [[0, 2400.5], [300, 2391.0]], signals=[
        {"at": 0,  "channel": "Debug Channel", "text": SIGNAL_TEXT},
        {"at": 10, "channel": "Debug Channel", "text": SECOND_SIGNAL},
    ])
    fresh_db.update_risk_settings({
        "auto_execute_signals": 1, "accept_tg_signals": 1,
        "max_daily_loss_pct": 0,         # breaker disarmed
    })
    reader.feed_due(now=1.0)
    asyncio.run(engine._scan_messages())

    clock["now"] = BASE + 300
    asyncio.run(_cycles(engine))
    assert engine.is_trading_paused() is False

    reader.feed_due(now=10.0)
    new_signals = asyncio.run(engine._scan_messages())

    assert any(s.get("auto_executed") for s in new_signals)


# ─────────────────────────────────────────────────────────────────────────────
# 010 — a slow EA is not a failed EA
# ─────────────────────────────────────────────────────────────────────────────

class _SlowEA:
    """The EA in the exact state that caused the 2026-07-30 runaway: it PUT THE
    ORDER ON THE BOOK, then took too long to say so.

    `placed_on_book=False` makes it a genuinely failed handoff instead, which
    is what the control below needs.
    """

    def __init__(self, bridge, placed_on_book: bool = True):
        self._bridge = bridge
        self._placed_on_book = placed_on_book
        self.asked = 0

    def is_ea_healthy(self) -> bool:
        return True

    def is_strategy_portable(self, strategy) -> bool:
        return True

    async def open_trade(self, trade_id, direction, lots, sl, tps, strategy, **kw):
        self.asked += 1
        if self._placed_on_book:
            # The EA's own comment format -- this is what makes the order
            # findable by the trade id later.
            await self._bridge.place_order(
                direction, lots, sl, None, f"ea:{trade_id[:COMMENT_ID_LEN]}",
            )
        raise asyncio.TimeoutError("ack never arrived")


def _install_ea(monkeypatch, ea) -> None:
    from backend.src.services.broker import ea_bridge as _ea_mod
    monkeypatch.setattr(_ea_mod, "get_instance", lambda: ea)


def test_010_a_slow_ea_ack_does_not_place_a_second_order(fresh_db, monkeypatch, caplog):
    """
    Terminal demo this stands in for:
        1. Pause the EA in the MT5 terminal so its ack cannot get back in time.
        2. Send a signal.
        3. Expect: EXACTLY ONE position on the account, and the log shows the
           fallback adopting the EA's order rather than sending its own.

    Before stage3/010 nothing asked the broker, and the two send paths stamped
    different identifiers, so no check was even possible. Five signals became
    roughly 133 opens.
    """
    clock = {"now": BASE}
    engine, reader = _runtime(clock, [[0, 2400.5]])
    ea = _SlowEA(engine._bridge, placed_on_book=True)
    _install_ea(monkeypatch, ea)
    fresh_db.update_risk_settings({
        "auto_execute_signals": 1, "accept_tg_signals": 1,
        "ea_bridge_enabled": 1,
    })
    reader.feed_due(now=1.0)

    with caplog.at_level("WARNING"):
        new_signals = asyncio.run(engine._scan_messages())

    assert ea.asked == 1, "the EA must actually have been asked, or 010 is not under test"
    assert new_signals and new_signals[0]["auto_executed"] is True

    # THE assertion. Two positions here is the bug.
    positions = asyncio.run(engine._bridge.get_positions())
    assert len(positions) == 1, f"a duplicate order was placed: {positions}"

    # And the database points at the EA's real ticket, not a phantom.
    with fresh_db.db() as conn:
        row = conn.execute(
            "SELECT status, mt5_ticket, entry_price, managed_by "
            "FROM vantage_simulated_trades"
        ).fetchone()
    assert row["status"] == "open"
    assert row["mt5_ticket"] == positions[0]["ticket"]
    assert row["managed_by"] == "ea", "an adopted EA order must stay EA-managed"
    assert float(row["entry_price"]) > 0, "entry 0 is the placeholder shape from bugs/016"

    assert any("adopted existing broker order" in r.getMessage() for r in caplog.records)


def test_010_control_a_genuinely_failed_handoff_still_sends(fresh_db, monkeypatch):
    """Negative control: same timeout, but the EA never got the order onto the
    book. The fallback MUST send, or a slow-EA fix would have silently broken
    every real failover."""
    clock = {"now": BASE}
    engine, reader = _runtime(clock, [[0, 2400.5]])
    ea = _SlowEA(engine._bridge, placed_on_book=False)
    _install_ea(monkeypatch, ea)
    fresh_db.update_risk_settings({
        "auto_execute_signals": 1, "accept_tg_signals": 1,
        "ea_bridge_enabled": 1,
    })
    reader.feed_due(now=1.0)
    asyncio.run(engine._scan_messages())

    positions = asyncio.run(engine._bridge.get_positions())
    assert len(positions) == 1, "the fallback should have placed the order"
    with fresh_db.db() as conn:
        row = conn.execute(
            "SELECT status, mt5_ticket, managed_by FROM vantage_simulated_trades"
        ).fetchone()
    assert row["status"] == "open"
    assert row["mt5_ticket"] == positions[0]["ticket"]
    assert row["managed_by"] == "python"


# ─────────────────────────────────────────────────────────────────────────────
# 020 — no answer is not the same as "no"
# ─────────────────────────────────────────────────────────────────────────────

def _blind_the_broker(bridge) -> None:
    """Make every lookup raise, the way a dropped bridge connection does.
    An UNREACHABLE broker has not said the trade is absent."""
    async def _boom(*_a, **_kw):
        raise ConnectionError("bridge socket is gone")
    bridge.get_positions = _boom          # type: ignore[method-assign]
    bridge.get_deal_history = _boom       # type: ignore[method-assign]


def test_020_an_unanswered_send_parks_the_signal_instead_of_retrying(
        fresh_db, monkeypatch, caplog):
    """
    Terminal demo this stands in for:
        1. Send a signal with the EA paused AND the bridge connection pulled.
        2. Expect: the signal shows as 'unknown', NOT 'pending'; nothing is
           re-sent; the scheduler leaves it alone until reconciliation.

    'pending' here is the dangerous state: PendingWatcher re-activates a
    pending signal every 20 seconds, so a filled-but-unconfirmed order gets
    sent again, and again.
    """
    clock = {"now": BASE}
    engine, reader = _runtime(clock, [[0, 2400.5]])
    ea = _SlowEA(engine._bridge, placed_on_book=True)
    _install_ea(monkeypatch, ea)
    fresh_db.update_risk_settings({
        "auto_execute_signals": 1, "accept_tg_signals": 1,
        "ea_bridge_enabled": 1,
    })
    reader.feed_due(now=1.0)

    positions_before = asyncio.run(engine._bridge.get_positions())
    _blind_the_broker(engine._bridge)

    with caplog.at_level("ERROR"):
        asyncio.run(engine._scan_messages())

    with fresh_db.db() as conn:
        signal = conn.execute(
            "SELECT signal_id, status, notes FROM vantage_signals"
        ).fetchone()
        trades = conn.execute(
            "SELECT COUNT(*) c FROM vantage_simulated_trades"
        ).fetchone()["c"]

    assert signal["status"] == "unknown", (
        "'pending' here is what turns one filled order into several"
    )
    assert "unknown" in (signal["notes"] or "").lower() or signal["notes"]
    assert trades == 0, "nothing may be recorded as open when nobody knows"

    assert any("UNKNOWN" in r.getMessage() for r in caplog.records)

    # The scheduler must not be able to pick it up again: the atomic claim only
    # accepts 'pending' or 'active'.
    from backend.src.services.trading import signal_state_repo as ssr
    assert not ssr.claim_signal_activation(signal["signal_id"]), (
        "an unknown signal was handed back to the scheduler"
    )

    # And the EA was asked exactly once -- no silent re-send.
    assert ea.asked == 1
    assert positions_before == []


def test_020_control_a_real_rejection_still_goes_back_to_pending(
        fresh_db, monkeypatch):
    """Negative control: when the broker ANSWERS and says no, nothing filled,
    so the signal must stay retryable. Parking everything would be safe and
    useless -- it would stop the app trading after any rejection."""
    clock = {"now": BASE}
    engine, reader = _runtime(clock, [[0, 2400.5]])
    fresh_db.update_risk_settings({
        "auto_execute_signals": 1, "accept_tg_signals": 1,
    })
    reader.feed_due(now=1.0)

    # A definite refusal from a reachable broker.
    engine._bridge.inject_error(
        "place_order", {"error": "Invalid stops"}, count=10)

    asyncio.run(engine._scan_messages())

    with fresh_db.db() as conn:
        signal = conn.execute(
            "SELECT status FROM vantage_signals").fetchone()
        trades = conn.execute(
            "SELECT COUNT(*) c FROM vantage_simulated_trades").fetchone()["c"]

    assert signal["status"] == "pending", "a rejection is information: retrying is safe"
    assert trades == 0


# ─────────────────────────────────────────────────────────────────────────────
# 030 — the position nobody has a row for
# ─────────────────────────────────────────────────────────────────────────────

def test_030_an_orphan_position_is_reported_once_and_nothing_is_written(
        fresh_db, monkeypatch, caplog):
    """
    Terminal demo this stands in for:
        1. Send a signal, and kill the app between the order reaching the
           broker and the row reaching the database.
        2. Restart.
        3. Expect: the position is identified as ours, reported exactly once,
           and NOTHING is closed, opened or written on its account.

    This continues 020's scenario rather than inventing a new one -- an
    unanswered send is exactly how you end up with a position and no row.

    The repairers are deliberately NOT built (they would write, and route
    through the frozen close path), so "adopted" here means "named in the
    report", not "acted on".
    """
    from backend.src.services.positions import reconciliation as rec

    clock = {"now": BASE}
    engine, reader = _runtime(clock, [[0, 2400.5]])
    ea = _SlowEA(engine._bridge, placed_on_book=True)
    _install_ea(monkeypatch, ea)
    fresh_db.update_risk_settings({
        "auto_execute_signals": 1, "accept_tg_signals": 1,
        "ea_bridge_enabled": 1,
    })
    reader.feed_due(now=1.0)

    real_positions = engine._bridge.get_positions
    real_deals = engine._bridge.get_deal_history
    _blind_the_broker(engine._bridge)
    asyncio.run(engine._scan_messages())

    # The crash is over; the bridge is back. The broker holds a position this
    # app placed and has no row for.
    engine._bridge.get_positions = real_positions        # type: ignore[method-assign]
    engine._bridge.get_deal_history = real_deals         # type: ignore[method-assign]

    orphans = asyncio.run(engine._bridge.get_positions())
    assert len(orphans) == 1, "the demo needs exactly one un-rowed position"
    with fresh_db.db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM vantage_simulated_trades").fetchone()["c"] == 0

    with caplog.at_level("WARNING"):
        diff = asyncio.run(rec.collect_and_report(engine._bridge))

    assert diff is not None
    ours = diff.of_kind(rec.BROKER_ONLY_OURS)
    assert len(ours) == 1, f"expected one orphan, got {diff.entries}"
    assert ours[0].ticket == orphans[0]["ticket"]
    assert not diff.of_kind(rec.BROKER_ONLY_MANUAL), (
        "our own order was misread as somebody's manual trade"
    )

    # Read-only: the broker still holds it, untouched, and no row appeared.
    assert asyncio.run(engine._bridge.get_positions()) == orphans
    with fresh_db.db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) c FROM vantage_simulated_trades").fetchone()["c"] == 0
        assert conn.execute(
            "SELECT status FROM vantage_signals").fetchone()["status"] == "unknown"

    # Run it again: the same one orphan, not two. A reconciler that
    # double-counted would be worse than none.
    again = asyncio.run(rec.collect_and_report(engine._bridge))
    assert len(again.of_kind(rec.BROKER_ONLY_OURS)) == 1


def test_030_control_a_recorded_trade_is_matched_not_flagged(fresh_db, monkeypatch):
    """Negative control: the ordinary case must come out clean. A reconciler
    that flagged every position would be noise, and noise gets ignored."""
    from backend.src.services.positions import reconciliation as rec

    clock = {"now": BASE}
    engine, reader = _runtime(clock, [[0, 2400.5]])
    fresh_db.update_risk_settings({
        "auto_execute_signals": 1, "accept_tg_signals": 1,
    })
    reader.feed_due(now=1.0)
    asyncio.run(engine._scan_messages())

    diff = asyncio.run(rec.collect_and_report(engine._bridge))

    assert diff is not None
    assert len(diff.of_kind(rec.MATCHED)) == 1
    assert not diff.of_kind(rec.BROKER_ONLY_OURS)
    assert not diff.of_kind(rec.DB_ONLY_NO_EVIDENCE)
    assert diff.needs_attention is False
