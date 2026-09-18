"""Row builders for the analytics repo tests.

Deliberately explicit rather than clever. Every test states the columns it
depends on, so a query that quietly starts reading a different column fails on
a missing value instead of silently matching a default somebody else chose.

Nothing here reaches a broker: these write rows to the temporary SQLite file
`fresh_db` creates and nothing else.
"""
from __future__ import annotations

from backend.src.db import database as db_module

# A fixed instant well inside any window a test asks for. Never `time.time()`:
# a suite that takes four minutes would otherwise have tests whose "now" drifts
# out from under a cutoff computed at import.
NOW = 1_750_000_000.0
DAY = 86_400.0


def trade(
    trade_id: str,
    *,
    ticket=None,
    signal_id: str | None = None,
    tg_source: str | None = None,
    strategy: str = "scale_out",
    direction: str = "BUY",
    order_type: str = "market",
    pending_placed_at: float | None = None,
    max_tp_hit: str | None = None,
    open_time: float = NOW,
    status: str = "closed",
    entry_price: float = 2431.20,
    lot_size: float = 0.05,
) -> None:
    """One row in vantage_simulated_trades.

    `order_type` and `strategy` default to the schema's own defaults ("market",
    "scale_out") because both columns are NOT NULL. A test that cares about
    either says so explicitly.

    The price columns are NOT NULL in the schema and no query under test reads
    them, so they carry one realistic XAUUSD entry rather than zeros — a zero
    gold price in a fixture is the kind of thing that reads as real data three
    months later.
    """
    # signal_id is NOT NULL in the schema. Defaulting it to the trade_id keeps
    # every row valid without making each test state one it does not care
    # about — and the signal-id lookups that DO care pass their own.
    sid = signal_id if signal_id is not None else trade_id
    with db_module.db() as conn:
        known = conn.execute(
            "SELECT 1 FROM vantage_signals WHERE signal_id = ?", (sid,)).fetchone()
    if not known:
        signal(sid, source_name=tg_source, direction=direction, created_at=open_time)
    with db_module.db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades "
            "(trade_id, signal_id, mt5_ticket, direction, tg_source, strategy, "
            " order_type, pending_placed_at, max_tp_hit, open_time, status, "
            " entry_low, entry_high, entry_price, lot_size, remaining_lots, "
            " stop_loss, tp1) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trade_id, sid, ticket, direction, tg_source, strategy,
             order_type, pending_placed_at, max_tp_hit, open_time, status,
             entry_price - 0.5, entry_price + 0.5, entry_price, lot_size,
             lot_size, entry_price - 10.0, entry_price + 8.0),
        )


def signal(signal_id: str, *, source_name: str | None = None,
           direction: str = "BUY", created_at: float = NOW) -> None:
    """One row in vantage_signals.

    `vantage_simulated_trades.signal_id` is a foreign key, so every trade needs
    one of these behind it. `trade()` creates it automatically; call this
    directly only when a test needs the signal to differ from its trade.

    Deliberately NOT `INSERT OR IGNORE`. The first version of this helper used
    it, and `source_name` is `TEXT NOT NULL DEFAULT ''` — so passing None made
    SQLite skip the row in silence, and the failure surfaced two calls later as
    an unexplained FOREIGN KEY error on the trade. A seed helper that can fail
    quietly is a fixture that lies about what it built.
    """
    with db_module.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals "
            "(signal_id, source_name, direction, entry_low, entry_high, "
            " stop_loss, tp1, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (signal_id, source_name or "", direction, 2430.7, 2431.7,
             2421.2, 2439.2, created_at),
        )


def leg(trade_id: str, *, tier: int, ticket=None) -> None:
    """One row in vantage_ladder_legs, belonging to `trade_id`."""
    with db_module.db() as conn:
        conn.execute(
            "INSERT INTO vantage_ladder_legs "
            "(trade_id, tier, tp_num, tp_price, lots, entry_price, mt5_ticket) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (trade_id, tier, tier, 2440.0 + tier, 0.02, 2431.20, ticket),
        )


def dpm_row(trade_id: str) -> None:
    """A dpm_trade_performance row for `trade_id`, so the LEFT JOIN in
    `ticket_strategies` has something to find."""
    with db_module.db() as conn:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(dpm_trade_performance)").fetchall()]
        assert "trade_id" in cols, (
            "dpm_trade_performance has no trade_id column — the join this seeds "
            "for cannot be what the repo describes"
        )
        conn.execute(
            "INSERT INTO dpm_trade_performance (trade_id) VALUES (?)", (trade_id,))


def tg_signal(
    tg_message_id: int,
    *,
    group_id: str = "-100123",
    group_name: str = "Gold Signals",
    direction: str | None = "BUY",
    signal_id: str | None = None,
    entry_low: float | None = 2430.0,
    entry_high: float | None = 2432.0,
    stop_loss: float | None = 2420.0,
    tp1: float | None = 2440.0,
    tp2: float | None = None,
    parsed_at: float = NOW,
) -> None:
    """One row in vantage_tg_signals — a parsed Telegram signal."""
    with db_module.db() as conn:
        conn.execute(
            "INSERT INTO vantage_tg_signals "
            "(tg_message_id, group_id, group_name, raw_text, parsed_at, direction, "
            " entry_low, entry_high, stop_loss, tp1, tp2, signal_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (tg_message_id, group_id, group_name, "XAUUSD BUY 2430-2432",
             parsed_at, direction, entry_low, entry_high, stop_loss, tp1, tp2,
             signal_id),
        )


def tg_reply(
    telegram_message_id: int,
    *,
    reply_to: int,
    text: str,
    group_id: str = "-100123",
    timestamp: float = NOW,
) -> None:
    """One row in telegram_messages, replying to `reply_to`."""
    with db_module.db() as conn:
        conn.execute(
            "INSERT INTO telegram_messages "
            "(telegram_message_id, group_id, text, timestamp, reply_to_message_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (telegram_message_id, group_id, text, timestamp, reply_to),
        )


def closed_trade(
    trade_id: str,
    *,
    signal_id: str,
    net_pnl: float,
    exit_reason: str = "TP",
    close_time: float = NOW + 3600,
    open_time: float = NOW,
    strategy: str = "scale_out",
    entry_price: float = 2431.0,
    close_price: float = 2440.0,
    lot_size: float = 0.10,
    ticket=None,
) -> None:
    """A closed vantage_simulated_trades row wired to an existing signal."""
    with db_module.db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET status = 'closed' WHERE trade_id = ?",
            (trade_id,),
        )
    trade(trade_id, ticket=ticket, signal_id=signal_id, strategy=strategy,
          open_time=open_time, status="closed", entry_price=entry_price,
          lot_size=lot_size)
    with db_module.db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades "
            "SET net_pnl = ?, exit_reason = ?, close_time = ?, close_price = ? "
            "WHERE trade_id = ?",
            (net_pnl, exit_reason, close_time, close_price, trade_id),
        )


def partial_close(trade_id: str, *, lots: float, price: float, pnl: float,
                  ts: float = NOW + 60, reason: str = "TP1") -> None:
    with db_module.db() as conn:
        conn.execute(
            "INSERT INTO vantage_partial_closes "
            "(trade_id, ts, lots_closed, close_price, pnl, reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (trade_id, ts, lots, price, pnl, reason),
        )


def dpm_perf(trade_id: str, *, closed_at: float = NOW + 3600,
             r_multiple: float = 1.5, exit_type: str = "trail",
             peak_pnl: float = 20.0, final_pnl: float = 15.0,
             regime: str = "trend", session: str = "London",
             used_calibrated: int = 1) -> None:
    """A dpm_trade_performance row — what marks a trade as DPM-managed."""
    with db_module.db() as conn:
        conn.execute(
            "INSERT INTO dpm_trade_performance "
            "(trade_id, closed_at, r_multiple, exit_type, peak_pnl, final_pnl, "
            " regime_at_entry, session_at_entry, used_calibrated) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (trade_id, closed_at, r_multiple, exit_type, peak_pnl, final_pnl,
             regime, session, used_calibrated),
        )
