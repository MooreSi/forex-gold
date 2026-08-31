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

## Three states, not two: found / absent / UNKNOWN

**A broker that could not be asked has not said no.**

This is the rule the 2026-08-29/31 money-path work is built on, and it is the
one most easily lost in a "simplification". Every place the app asks the broker
a question, the answer has three shapes:

| | Meaning | Safe to act? |
|---|---|---|
| **found** | the broker has it | adopt it; never send again |
| **absent** | a reachable broker says it does not have it | yes — sending is safe |
| **unknown** | the question could not be asked, or the answer was lost | **no** |

Collapsing `unknown` into `absent` is exactly how a retry doubles an order.
That is not hypothetical: on 2026-07-30 five signals became roughly 133 opens
and 36 live positions the app could not see.

Concretely:

- `broker/dedup.py::find_trade` returns all three. Callers must branch on all
  three; `safe_to_send` is only true when it is neither found nor unknown.
- `mt5_bridge._place_order` **does not retry when `order_send` returns `None`.**
  A lost response is not a rejection; walking on to the next filling mode sends
  a second order.
- A signal whose send got no answer parks in `unknown` status. **Not `pending`**
  — the scheduler re-activates a pending signal every 20 seconds. Only
  reconciliation may resolve `unknown`, from broker truth.
- A close is recorded **only** on a confirmed broker close. A refusal, an
  exception or a response carrying neither `success` nor `error` leaves the row
  open, says so loudly, and lets reconciliation settle it. The trade stays
  managed in the meantime.

### Gate the funnel, not the callers

020's fix was written into one signal route and not the parallel one, so on the
route most signals actually take it never ran. 010 and 050 are sound because
they gate a **single funnel**: `bridge.place_order` has exactly one call site
in the tree, and the protective-halt check sits in `open_trade` above both send
paths.

Both properties are pinned by
`tests/refactor/test_order_paths_have_one_funnel.py`. Where a check is repeated
per route, a route is eventually missed.

## The reconciliation contract

`services/positions/reconciliation.py` compares broker truth against the
database every 12 monitor cycles. Two things about it are deliberate:

- **It writes nothing.** Not to the broker, not to the database. Asserted by a
  structural test that walks the AST for write calls. The repairers are not
  built, because they would route through the frozen close path.
- **Absence of evidence is not evidence.** A row the broker has no record of is
  reported as `DB_ONLY_NO_EVIDENCE` and left open — a failed broker read looks
  identical to a genuinely closed trade, and booking one shut on a guess is the
  same mistake as recording an unconfirmed close.

A position carrying no order id in its comment is `BROKER_ONLY_MANUAL`: it
counts toward exposure, and the app must never touch it — no stop moved, no
close.
