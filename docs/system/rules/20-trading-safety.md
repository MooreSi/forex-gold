# Trading safety — the parts that move real money

This app connects to a live MetaTrader 5 account and places real orders. This
page is the map of which code can cost money and what the rules are around
each piece.

## The order-touching surfaces

| Surface | Where | What it can do |
|---|---|---|
| **Open** | `services/trading/open_trade.py`, `instant_entry.py`, `limit_order_signal.py` | places a real order |
| **Close** | `services/trading/close_trade.py`, `partial_close.py` | closes a real position |
| **Modify** | `services/broker/repo.py` (SL/TP writes), the strategy handlers | moves a live stop |
| **Sizing** | `services/risk/governor.py`, `suggest_lot_size` | decides how much money is at risk |
| **The bridge** | `mt5_bridge.py`, `services/broker/ea_bridge.py` | the actual wire to MT5 |

Any change inside these needs owner sign-off and a demo-account session.
Reading them is free; changing them is not.

## The close path is frozen

`close_trade`, `record_close`, `_make_close_trade_ctx`, `partial_close_trade`,
`_schedule_profit_sync`.

Relocating them verbatim is allowed. Reshaping them — an argument added,
removed, reordered or defaulted, a branch restructured, an early return moved
— is not, without sign-off plus a demo session.

The witness is `tests/core/test_close_trade_characterization.py`. It must pass
**unmodified**. During the entire 2026 refactor it did, through every batch.
If your change needs it edited, your change is out of scope.

## Test rules for these surfaces

**No test may place, close or modify a real or demo order. Ever.**

- Bridges are fakes returning canned dicts.
- Order calls are sentinels that record and return.
- A test file that *could* reach a real call says so in its docstring and
  asserts that it cannot.

If you cannot see how to test a change without a broker, that is the signal to
stop, not to improvise.

## Things that gate order placement

These decide whether a trade happens at all. Changing them changes the risk
envelope even when the code looks harmless:

- **Minimum TP1 R:R** (`min_tp1_rr`, default 0.75) — thinner setups are skipped
- **Risk Governor R:R floor** (`rg_min_tp1_rr`, 1.00)
- **Max stop width** (`rg_max_stop_atr`, 1.5 × ATR)
- **Directional cap** (`max_unprotected_trades`, 2) — correlated exposure limit
- **Max signal age** (`max_signal_age_s`, 240s) — raising this is how a
  backfilled signal fills minutes late at a worse price. It has happened.
- **Broker-close miss threshold** (`mt5_sync_miss_threshold`, 2) — at 1, a
  single dropped request looks like a closed trade

All are now in **Settings → Expert Tunables**, clamped to safe ranges.
Defaults are byte-identical to the constants they replaced.

## Known-dangerous history

Incidents that shaped the current code. Do not undo these:

- **Telegram lot-sizing fork** — Telegram entries bypassed the Max Risk per
  trade ceiling. Fixed; sizes changed as a result and still want eyeballing.
- **Demo/live cache staleness** — caches keyed on time alone kept serving the
  previous environment's risk settings for 10s after switching account. Every
  cache now registers an invalidator.
- **Non-atomic signal insert** — a signal could be half-written. Now in a
  `transaction()`.
- **Duplicate MT5 orders on paired nodes** — a follow-up signal opened a
  second independent trade because the first lived only in the VPS's database.
  The forwarding condition in `open_trade()` and its follow-up must stay in
  sync.
- **Both nodes running AI fallback** — doubled AI cost and split the review
  queue across two databases. `is_active_trader_node()` gates it.
- **Bridge restart storm** — a single slow health check triggered a real
  bridge restart, causing the disconnects it was meant to prevent. Now
  requires two consecutive failures 60s apart.

Each has a comment at the site with the date. Read it before "simplifying".

## The two-node setup

A Mac and a VPS can be paired. Only one is the active trader at a time.

- `is_active_trader_node()` — may this node execute trades?
- `is_bot_command_authority()` — may this node poll the Telegram bot?

Both **fail open** (return True) for a standalone install, deliberately: an
unpaired machine has no counterpart to conflict with, and an error here must
not silently kill trading or bot control.

Only one process may long-poll a bot token; a second gets 409 Conflict. The
authority check plus the 409 back-off in `services/telegram/bot_loop.py` is
what stops the two nodes kicking each other in a loop.
