"""Offline end-to-end: scripted signal → open → manage → close, on fakes.

This is the proof the refactor is alive — the reason debug mode exists.
A Telegram-shaped message from a scripted scenario flows through the REAL
pipeline: FakeTelegramReader buffer → scan_messages → parser →
auto-execute → order placement → FakeMT5Bridge ledger → monitor cycle →
TP management → the frozen close path → close row in the DB. Nothing in
the pipeline is mocked; only the two external boundaries are fakes.

Deviation from the pack's sketch, on purpose: backend.src.app.startup()
is not booted (it spawns engines, watchdogs and long-lived loops that a
test must then unwind); the runtime facade is driven directly, which is
the same code path minus the task supervisors.

No real or demo MT5 order can be placed here: the bridge is FakeMT5Bridge
(no network code), the reader is FakeTelegramReader (no Telethon), and
the runtime is constructed with an empty bridge URL config.
"""
from __future__ import annotations

import asyncio

from backend.src.runtime import TradingRuntime
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


def _build(clock: dict):
    """A runtime on fakes: price rides 2400.5 → 2412 (TP1 2410 crossed)
    then collapses to 2392."""
    scenario = {"anchors": [[0, 2400.5], [300, 2412.0], [600, 2392.0]]}
    engine = TradingRuntime(CONFIG)
    engine._bridge = FakeMT5Bridge(
        seed=1, scenario=scenario, base_ts=BASE,
        clock=lambda: clock["now"], starting_balance=1000.0,
    )
    reader = FakeTelegramReader(CONFIG, scenario={"signals": [
        {"at": 0, "channel": "Debug Channel", "text": SIGNAL_TEXT},
    ]})
    engine.set_telegram_reader(reader)
    return engine, reader


async def _cycles(engine, n: int = 3) -> None:
    for _ in range(n):
        await run_monitor_cycle(engine._make_monitor_ctx())


def test_signal_to_close(fresh_db):
    clock = {"now": BASE}
    engine, reader = _build(clock)
    fresh_db.update_risk_settings({"auto_execute_signals": 1, "accept_tg_signals": 1})
    reader.feed_due(now=1.0)

    # 1. Scripted message → real parser → auto-executed on the fake bridge.
    new_signals = asyncio.run(engine._scan_messages())
    assert len(new_signals) == 1
    assert new_signals[0]["auto_executed"] is True

    with fresh_db.db() as conn:
        trade = conn.execute(
            "SELECT trade_id, status, entry_price, mt5_ticket, remaining_lots "
            "FROM vantage_simulated_trades"
        ).fetchone()
    assert trade["status"] == "open"
    assert trade["mt5_ticket"] == 80000001            # first fake ticket
    assert 2399.0 <= trade["entry_price"] <= 2402.0   # filled inside the zone
    positions = asyncio.run(engine._bridge.get_positions())
    assert [p["ticket"] for p in positions] == [80000001]

    # 2. Price rides to 2412 — TP1 (2410) crossed; the monitor manages it.
    clock["now"] = BASE + 300
    asyncio.run(_cycles(engine))

    with fresh_db.db() as conn:
        row = conn.execute(
            "SELECT status, remaining_lots, sl_moved_to_be, exit_reason, net_pnl "
            "FROM vantage_simulated_trades"
        ).fetchone()
        partials = [dict(r) for r in conn.execute(
            "SELECT lots_closed, reason, pnl FROM vantage_partial_closes")]

    assert partials and partials[0]["reason"] == "TP1"
    assert partials[0]["pnl"] > 0
    assert row["sl_moved_to_be"] == 1                 # scale-out moved SL to BE
    # The whole 0.01 position closes at TP1 (broker-minimum lot cannot split).
    assert row["status"] == "closed"
    assert row["exit_reason"] == "all_tps_hit"
    assert row["net_pnl"] > 0

    # 3. The fake ledger agrees: position gone, realised profit in balance.
    assert asyncio.run(engine._bridge.get_positions()) == []
    account = asyncio.run(engine._bridge.get_account())
    assert account["balance"] > 1000.0

    # 4. Deal history shows the round trip (open IN + close OUT).
    deals = asyncio.run(engine._bridge.get_deal_history(1))
    entries = sorted(d["entry"] for d in deals if d["position_id"] == 80000001)
    assert entries == [0, 1]


def test_sl_path_closes_and_records_the_loss(fresh_db):
    """The unhappy path: no TP is reached, price collapses through the SL,
    the monitor records the close with a negative P&L."""
    clock = {"now": BASE}
    scenario = {"anchors": [[0, 2400.5], [300, 2391.0]]}
    engine = TradingRuntime(CONFIG)
    engine._bridge = FakeMT5Bridge(
        seed=1, scenario=scenario, base_ts=BASE,
        clock=lambda: clock["now"], starting_balance=1000.0,
    )
    reader = FakeTelegramReader(CONFIG, scenario={"signals": [
        {"at": 0, "channel": "Debug Channel", "text": SIGNAL_TEXT},
    ]})
    engine.set_telegram_reader(reader)
    fresh_db.update_risk_settings({"auto_execute_signals": 1, "accept_tg_signals": 1})
    reader.feed_due(now=1.0)
    asyncio.run(engine._scan_messages())

    clock["now"] = BASE + 300   # mid 2391 — below the 2392 stop
    asyncio.run(_cycles(engine))

    with fresh_db.db() as conn:
        row = conn.execute(
            "SELECT status, exit_reason, net_pnl FROM vantage_simulated_trades"
        ).fetchone()
    assert row["status"] == "closed"
    assert row["exit_reason"] == "SL"
    assert row["net_pnl"] < 0
