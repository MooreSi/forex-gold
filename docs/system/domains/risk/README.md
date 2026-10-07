# Risk

**Living file — update when this domain teaches you something.**
Covers: `backend/src/services/risk/`.

## What it is

A deterministic, app-wide safety layer that decides whether a trade may be
placed and how large it may be — it never places or modifies orders itself,
and its **failure direction is refuse-to-trade**. It comprises the Tier-1
Risk Governor (risk-% sizing, hard per-trade $ ceiling, daily-loss halt,
loss-streak cooldown, R:R floor, directional cap, stop-width cap), the
per-day/per-window Trading Schedule that caps over-trading once a profit
target is hit (this is what implements the "£300 a day then stop" goal — see
`../../vision/000-goal.md`), a session gate, a circuit breaker on
consecutive losses, and the configuration surfaces (risk settings,
per-strategy parameters, Expert Tunables, custom strategies, retention).

## Where the code lives

- `services/risk/governor.py` — `is_trading_paused`, `check_pre_trade_filters`, `rg_size_and_check`, `rg_check_halt`, `rg_apply_halts_on_close`, `RR_BYPASS_SOURCES`
- `services/risk/schedule.py` — the 7×3 per-day/per-window profit-target gate with per-source keys
- `services/risk/risk_settings_repo.py` — risk settings with a 10s TTL cache, `get_effective_strategy`, `is_session_allowed`
- `services/risk/circuit_breaker_repo.py` — circuit-breaker state persisted in `vantage_risk_settings`
- `services/risk/strategy_params.py` — live-tunable per-strategy SL-shaping constants + named template library
- `services/risk/expert_params.py` — Tier-A behaviour constants (~135), defaults in code, overrides as JSON in `app_config`
- `services/risk/settings.py` — the settings surface six pages share
- `services/risk/app_config.py` / `app_config_repo.py` — the key/value store
- `services/risk/custom_strategies_repo.py` — user-defined strategies
- `services/risk/retention.py` — data-retention window and `switch_environment`
- `services/risk/repo.py` — remaining SQL (templates, realised-P&L sums)

## Constraints / must not change

- The domain gates trading, never places or modifies orders — failure direction is refuse-to-trade.
- **Manual orders are exempt by construction.** The schedule gate is wired in from `signals/resolution.py::resolve_open_trade_params`, reachable only from the automated `open_trade_from_signal` path. No special-casing anywhere else.
- Signal generation and Telegram ingestion are never affected by the schedule — it gates only the final automated "place an order" step.
- Gate ordering inside `resolve_open_trade_params`: session gate → Trading Schedule → `check_pre_trade_filters` → `rg_size_and_check`. Inside sizing: stop-width-vs-ATR cap first, then risk-% sizing; `rg_check_halt` applies daily loss limit and loss-streak cooldown.
- `expert_params.py` load-bearing properties (asserted by `tests/core/test_expert_params.py`): every default is **byte-identical** to the constant it replaced, and every value is **clamped** to a declared range — the clamp is a safety control ("a 0 in the R:R floor would open trades the system currently refuses"). Unknown keys are dropped, not stored.
- `settings.py::update` must not be bypassed: the repo write also forwards the change to the paired node and invalidates the TTL cache.
- `rg_apply_halts_on_close` stays wrapped in one outer `with db_module.db():` — a crash between its two config writes could leave a pause flag with no reason.
- `rg_check_halt` **must** be given the live MT5 balance, not the internal sim ledger — the sim ledger produced a false drawdown halt (confirmed 2026-07-07: $707 sim vs $1122 real).
- `strategy_params.py` scope is SL-shaping constants only — not TP-ladder close-% tables.

## Known things & gotchas

- **With SL Parsing off, a trade's realised risk is the template's `sl_pips` PLUS the distance from the fill to the far edge of the entry zone (2026-09-11, handover/032).** `keyword_triggers.apply_sl_parsing_override` anchors the derived stop to the far edge (BUY: `entry_low`, SELL: `entry_high`) deliberately, so the stop clears the whole zone whatever price the fill lands at and `validate_signal`'s direction checks pass. The cost is that risk per trade is not the template's number: measured over 261 live template trades in 14 days, the **median** realises exactly the template's distance (most fills land on the stop-adjacent edge, which is also the favourable one — 111 of 186), but 8% carry >=1.5x and one carried 2.4x ($120.30 against a template saying $70). With Fixed Lot Size nothing normalises it. **Do not read this as "losses exceed the stop"** — that is a different fault on a different path (reversal-engine/020); here the stop is honoured exactly and is simply wider than expected.
- **The drawdown watermark is account-scoped, and only the resolver enforces that (2026-09-10, bugs/042).** `peak_balance` lives in `app_config`, which `db/account_registry.py` copies as an install-wide table, so a second account inherited the first's watermark: 26004592 opened holding 25470480's $2,403.25 against a balance under $1,000, a 63% drawdown against a 40% limit. It did nothing only because the total-drawdown branch of `rg_check_halt` is reached solely from `rg_apply_halts_on_close`, which `close_trade` calls **only when `risk_governor_enabled`** — the daily-loss halt and give-back guard are called unconditionally, which is why those work with the governor off. Two consequences: the 40% limit is enforced on no close today, and switching the governor on would have halted trading on the first one. `resolve_db_path` now stamps `peak_balance_account` and clears the watermark when it does not match, so it re-anchors from the live balance on that account's next close. **The window this leaves:** between the clear and that close there is no watermark and the drawdown halt cannot fire — the same state a fresh install is in, and the safe direction, but it means the halt is only as old as the account's last close.
- **Out of Hours reads its window in `ooh_timezone`, not UTC, and has NO user interface again as of 2026-09-11 (handover/020).** It gained a card on 2026-09-07 and the owner removed it on 2026-09-11 (*"we already have a schedule which does the same thing"*) — which is not quite true: the schedule decides whether a trade is **opened**, this decides which strategy **manages one already open**. The removal is safe because `ooh_enabled` is 0 on the live install, not because the schedule covers it. Turning it on in the database starts a strategy no screen shows. `get_effective_strategy` is live — `monitor_cycle.py:206` picks the managing strategy with it — yet `ooh_enabled`, `ooh_start_time`, `ooh_end_time`, `ooh_strategy` and the holiday range are settable only by direct database edit. The timezone defaults to `""` (= UTC), byte-identical to the old behaviour, because a new default must retune nobody on upgrade AND because a machine-local default would make the Mac and the VPS start out disagreeing — the split the setting exists to prevent. An unusable zone falls back to UTC and warns; it must never raise, because this runs inside the monitor cycle. **Testing trap found here:** `get_effective_strategy` wraps its body in try/except and returns the base strategy on any error, which is indistinguishable from a correct UTC fallback — so bad-input tests routed through it pass even when the code raises. Assert on `_ooh_now` directly. Pinned by `tests/core/test_ooh_timezone.py`.
- **Per-source schedule toggles (2026-07-24):** each of the 7×3 windows independently gates Telegram / Reversal Engine / Breakout Engine, because Reversal Engine performs well overnight (Asia) but loses during London/NY — the opposite of the Telegram channels.
- **The daily profit target can be resumed past, for today only (2026-09-16, owner).** `trading_schedule_daily_target` refuses every automated entry for the rest of the day once the day's realised P&L clears it — and the header said "Circuit Breaker OK" throughout, the same false all-clear the news box was added to fix on 2026-09-04 but lasting hours instead of minutes. The badge now shows **Profit Target Reached** and clicking it opens the same Re-enable trading? dialog the breaker and the governor halt use. The resume is stored as a **day** (`trading_schedule_daily_target_resumed_day`, `YYYY-MM-DD` on the trading clock), not a flag, so it expires at midnight by construction — nothing clears it and nothing can leave it armed into tomorrow. It lifts the **daily** target only: the per-window targets, the window hours and the per-source toggles are separate gates and are untouched. It rides the existing schedule sync snapshot, and a snapshot with the key **absent** (a peer that predates the field) leaves it alone rather than clearing it, or every sync tick would re-arm a target the operator had deliberately resumed past. Badge state comes from `schedule.daily_profit_target_state()`, which reports `reached` only when the gate is actually holding orders — same contract as `news_pause_state`. Pinned by `tests/risk/test_daily_target_resume.py` and `tests/frontend/test_profit_target_badge.py`.
- Profit-per-window is computed **on demand** — `SUM(net_pnl)` of closed trades whose `open_time` falls in today's window — so it can't drift and needs no midnight reset.
- Two clocks in one domain: schedule times are local wall-clock HH:MM (matching the UI), while `is_session_allowed` maps sessions in **UTC** (Asia 21–07, London 07–12, overlap 12–16, NY 16–21).
- **`rr_filter_bypassed` has two arms and they are not interchangeable (2026-09-11, owner).** The static `RR_BYPASS_SOURCES` arm says "this channel supplies its own TP/SL levels"; the IME arm says "the user opted into taking this channel's fill the moment the signal lands". Only the second has a premise that staleness destroys, so `ignore_ime` (and `check_pre_trade_filters(ignore_ime_bypass=...)`) suspends **that arm alone**. Its only caller is the pending watcher's blind-gap re-validation — see `signals/gap_revalidation.py`. Worth knowing before touching either arm: IME is on globally on both the live and demo installs, so the IME arm currently bypasses the R:R floor for *every* Telegram channel with a `channel_parser_config` row, not just the two static ones.
- `RR_BYPASS_SOURCES` matching is case-insensitive and substring-based — the R:R floor is skipped entirely for paid-provider channels, including wrapped names like "Telegram Auto (Gold Diggers VIP)".
- `price_in_entry_range` is asymmetric on purpose: BUY zones are pullback areas, SELL zones are rally areas — the opposite side means chasing price.
- `is_session_allowed` imports `dpm.engine` *locally* to avoid a circular import — the session gate transitively depends on the DPM engine.
- Risk settings are served from a 10s TTL cache living on the `database` module itself; `update_risk_settings` carries a re-entrancy guard for sync-applied changes.
- `retention.switch_environment` is the genuinely dangerous call in that module: `db.init()` closes stale connections and flushes every registered cache, and the whole app then reads a different file.

## A pause is enforced in `open_trade` only; resting orders had to be taught it (2026-10-05, owner)

The daily goal, daily-loss halt, give-back guard and manual pause all write
`trade_pause_until`. `open_trade` is the one place that refused on it, so on a
goal-reached day with the badge reading "Trading Paused", trades kept opening
through routes that reach the EA without `open_trade`:

- the Limit Runner (`limit_order_signal.handle_limit_order_signal`), including
  its Entry Realignment market fallback -- now refuses first;
- the Reversal Engine LIMIT ORDER path (`_try_re_limit_order`) -- now records
  `limit_order_skip:trading paused` and returns handled;
- orders already resting at the broker when the halt landed, which MT5 fills
  with no round trip to Python -- `resting_revalidation.enforce_trading_pause`
  withdraws them every monitor cycle, and `_refusal_for` keeps them off while
  paused and re-arms them (original expiry) if the pause lifts.

Channel `"Manual"` (manual limit, Set & Forget, ORB) is exempt by the owner's
instruction. The withdraw side **fails open** on an unreadable pause (pulling
the whole book on a database blip is the outage this module rules out); the
placement side keeps `is_trading_paused`'s fail-closed behaviour. Pinned by
`tests/trading/test_pause_covers_limit_and_resting_orders.py`.

**Not covered: EA Template grid legs.** A grid template (Gold Diggers
Institutional runs one) stages its resting legs inside the EA, with leg tickets
Python does not hold, and they are not rows in `vantage_pending_orders`. A leg
placed before the halt can still fill after it. Pulling them needs an EA-side
command or a broker-order listing keyed to leg ids and a demo session; see
`_on_grid_leg_cancelled`. Unresolved.

## A goal pause is named as one, in the header and on Telegram (2026-10-05, owner)

The daily goal halts through the same `trade_pause_until` pair as the loss
guards, so the badge read "Trading Paused until ..." for a good day and a bad
one alike. `trading_status.halt_label` now reads "Goal Achieved Paused until
<dd Mon HH:MM>" when the halt reason starts with `GOAL_REASON_PREFIX`
("Daily goal ", which both `daily_goal` reasons do) and no circuit breaker is
in force. On a Mac it is derived from the VPS's `detail`, so a VPS on older
code still reads right.

Telegram alerts while paused come from one place, `risk/pause_message.py`,
led by that same headline. Before, an out-of-zone signal said "Signal queued
... Will auto-activate when price returns to zone" and an in-zone one said
"Auto-execution failed: Trading paused ...", though both rows were left
`pending` and neither opens while paused. `scan_auto_execute.
execute_auto_signal` rewords those two exits (and a grid template's refused
placement) after the fact; nothing about where a signal ends up changed. The
auto-execute-off and Limit Runner messages use the same headline. The brain
still files them under "halt" (its rule matches "paused"). Pinned by
`tests/risk/test_badge_names_a_reached_goal.py` and
`tests/trading/test_paused_signal_messages_agree.py`.

A manual Limit order is selectable through any pause (frontend gate
`limitOrderDisabledReason`); a manual Market order is not, because
`open_trade` refuses it. Whether it should is open: `docs/simon-handover/050`.

## Open questions

- The Expert Tunables clamp ranges are "documented guesses, flagged for review" — the bounds themselves are unvalidated.

## Capability switches (2026-09-11)

Migration 41 added a column per new capability from
`docs/todo/reversal-engine/200`, every one defaulting to off, and
`risk/capability_gates.py` is the only place they are read. Two properties
are pinned by tests and are load-bearing:

- a DEFAULT settings row leaves every gate inert, including `sizing_inputs`,
  which returns inputs that multiply a lot size by exactly 1.0
- a settings row MISSING those columns behaves the same, so a client that
  has not run migration 41 trades exactly as it did

New modules: `sizing_policy.py` (volatility, drawdown and correlated-exposure
adjustments that MODIFY a base lot size rather than computing one --
`fees_sizing.suggest_lot_size` stays the single sizing rule),
`session_liquidity.py` (rollover, weekend reopen, period end -- illiquidity on
a clock, which no news filter can see) and `event_tiers.py` (per-tier windows
composed over `news_calendar`'s events, wider AFTER a release than before it).

**`sizing_policy` is not wired to the order path.** That is deliberate and it
needs a demo session; see `docs/todo/reversal-engine/210`.

## Saving the Risk card writes every field on it (2026-09-11)

`save_risk()` in `frontend/pages/settings/_risk.py` writes back all
thirteen of its fields from whatever the form currently shows, and the
form is built once from a single `get_risk_settings()` read at render
time. If that read is stale, ticking one checkbox and pressing Save
silently reverts every other setting on the card.

Observed once, on a live demo account: turning on
`min_fill_delay_enabled` set `htf_bias_gate_enabled` back to 0, a setting
the owner had turned on two days earlier and the only one with a measured
positive edge on the account. Restored within a minute. A fresh page load
renders the card correctly, so it does not reproduce on demand -- which is
what makes it dangerous rather than merely annoying.

The fix is for the handler to re-read at save time and write only what
actually changed. Until that lands, treat any Save on this card as a write
to all of it and check the rest afterwards.


## The trend gate points the wrong way in the Asian session (2026-09-12)

`governor.htf_bias_blocks` was measured over the whole clock. Split by
session it inverts, over all 5,414 `re_signals` rows:

| session | with the bias | against it |
|---|---|---|
| asian (00-07 UTC) | n=693, **-$6.26**, CI [-9.40, -3.12] | n=621, -$0.50, CI [-3.69, 2.69] |
| every other | n=1,480, -$1.02, CI [-3.30, 1.27] | n=1,154, **-$4.81**, CI [-7.48, -2.14] |

Both bolded cells hold their sign across chronological halves; neither of
the other two does anything but straddle zero. So outside Asia the gate
refuses the cohort that loses $4.81 a trade, and inside it the gate refuses
the cohort that loses nothing while admitting the one that loses $6.26 --
about $5.76 a trade across 1,314 signals.

`capability_gates.asian_bias_exempt` (migration 45,
`htf_bias_asian_exempt`, **off**) stands the rule down for that window. It
does NOT invert it: -$0.50 with an interval straddling zero is not an edge.

Three things about it are load-bearing:

- **It is read in `capability_gates`, not inside `htf_bias_blocks`.** Six
  order routes share that function and the measurement above is Reversal
  Engine data. Only the Reversal Engine's live path consults the exemption;
  pinned by `tests/risk/test_htf_bias_gate_asian_exemption.py`.
- **The Reversal Engine refuses a counter-bias trade in TWO places** --
  the gate, and the original `level_score < 0.75` bypass
  (reversal-engine/090) beside it. While the gate is on the second is a
  strict subset of the first and changes no outcome; exempt only the first
  and the second becomes load-bearing again, silently turning the switch
  into "counter-bias in Asia, but only on strong levels". Both stand down
  together. A source-shape assertion cannot see this -- it survived the
  first mutation pass -- so it is pinned behaviourally in
  `tests/reversal_engine/test_asian_bias_exemption_on_the_live_path.py`.
- **The exemption requires the trend gate to be on.** With the gate off
  the level-score bypass is the only rule there is, and it predates this
  change; exempting it would disable something nobody asked about.

Caveat on the numbers: almost all of that P&L is the engine's virtual
ledger. Only a minority of signals reach the broker, so this is the
population the engine simulates, not a realised curve.

**The Bounce engine holds the opposite rule for the same hours** --
`test_signal/test_signal_generate.py` blocks counter-bias signals in the
Asian session specifically. Open question for the owner, recorded in
`docs/simon-handover/033`.

## Volatility targeting points the wrong way on this book (2026-09-17)

Measured before wiring `sizing_policy` to the order path, which is why it was
not wired. 5,116 closed `re_signals` rows carrying the H1 ATR at signal time:

| ATR decile | avg P&L |
|---|---|
| 1 (3.13–5.34) | -$4.89 |
| 2 (5.34–6.10) | -$6.53 |
| 5 (7.12–7.74) | **-$8.26** |
| 9 (10.21–12.80) | -$3.37 |
| 10 (12.80–29.98) | **-$2.25** |

The policy's premise is that violent markets are where the damage is. Here the
violent decile is the least bad and the quiet-to-middle deciles are the worst,
so the scalar sizes down into the best cohort and up into the worst. Replaying
every trade: -$22,318 actual, **-$23,431** with targeting, against a flat
control at the same total exposure of -$22,428. **-$1,002 worse than a flat
size change**, which rules out "it just traded more".

The drawdown arm is not a dial here at all. Peak equity on `re_balance_log` is
$511.53, reached early; the curve is now -$22,299 and **99.2% of entries sit
more than 20% below that peak**. `dd_full_pct` is 0.20, so the scalar is
pinned at its `MIN_DD_SCALAR` floor of 0.25 permanently. That is a standing
75% size cut, not a response to conditions — and `max_lot_size` is the honest
place to express that if it is ever wanted.

Recorded in
`docs/simon-handover/040-volatility-sizing-would-have-lost-more-not-less.md`.
The switch stays on the Reversal Engine Tuning card, off, connected to
nothing.

**A real bug found in the inert code and fixed in the same pass.**
`sizing_policy.apply` had no upper lot ceiling while `MAX_VOL_SCALAR` is 1.5,
and 98% of this account's trades are sized at exactly the 0.10 `max_lot_size`
cap — so a quiet market would have returned **0.15 lots, over a cap the user
set**. `SizingInputs.max_lots` now carries it, `capability_gates.sizing_inputs`
supplies it OUTSIDE the `scale_on` branch (it is the account's ceiling, not the
capability's), and `tests/risk/test_sizing_policy_respects_the_lot_ceiling.py`
holds the negative control that shows the 0.15. Nothing caught this because
`apply` still has no callers: an unwired module gets no test pressure from its
call sites, so its own tests are all it has.

## One per-trade size, and the EA template override (2026-09-25)

`services/risk/lot_sizing.py` is the one place that decides which number sizes
a trade (docs/todo/risk/010). Before it, five order paths each carried a copy
of the precedence, and they disagreed: on 2026-09-24 Gold Diggers VIP's
template (anchor 0.1) was capped by a Maximum lot size of 0 and the immediate
Telegram path sent MT5 **0 lots** twice ("invalid volume"), while the queued
path treated the same 0 as "size from risk %".

Load-bearing properties:

- **Risk % or Fixed lots, by construction.** Fixed lots is live exactly when
  `strategy_lot_size > 0` -- how every reader already read it. There is no
  mode column on purpose: the first version of this work added one, and 8
  existing tests (plus any paired node on an older build) that set
  `strategy_lot_size` alone stopped meaning "fixed lot wins". Choosing Risk %
  on the Risk tab writes 0 and parks the lot in `strategy_lot_size_parked`,
  which only the screen reads.
- **`global_sizing_override`** (off) makes every automated trade -- immediate,
  queued (all engines), limit, IME, grid legs -- use the global size instead of
  the template's `lot_anchor`/`risk_pct`. Risk % is the TOTAL for a grid
  signal, split across its legs; Fixed lots is per leg (owner, 2026-09-25). A
  lot a person typed (`lot_size_override`, manual orders) always wins. ORB and
  Set & Forget never call this module. With the override on, a lot or risk %
  the app stored on the signal (Telegram auto-sizing, the Breakout Engine's
  ML/Kelly lot) is ignored and re-sized.
- **The EA prefers a template's own leg lots.** `HandleOpenTemplateGrid` uses
  `tpl_lot_anchor`/`tpl_lot_pending` whenever they are non-zero and only falls
  back to the lot it is sent. `ea_template_for_lot` therefore rewrites the COPY
  sent to the EA whenever the template is not the source of the size -- which
  also fixes a template `risk_pct` that grids silently ignored.
- **A 0-lot trade is refused by name** at the top of `open_trade` (and in the
  limit path), before the stand-down check and before any bridge call. The
  queued path refuses too rather than falling back to risk %.
- **Settings validation** (`settings.update`) refuses a Maximum lot size below
  0.01, a fixed lot below 0.01 or above the maximum -- checking only the keys a
  save touches, so an old bad value never blocks an unrelated save.

Behaviour that changed with the override OFF, deliberately: a template
`risk_pct` now sizes the IME path (it was a 0.01 placeholder nothing
recomputed) and grid legs (the EA ignored it). No template on the live
install had `risk_pct > 0` on either path when this landed.

Needs a demo session before the override is switched on for the live account.

**A template trade is sized from the stop it is sent with (2026-09-29).** The
queued path (`resolve_open_trade_params`: every engine, and a Telegram signal
that arrives out of its zone) sized a template trade from the SIGNAL's stop,
then replaced that stop with the template's `sl_pips` stop before the order.
At Risk 2% on "30 TP1 SL50 and Trail" the owner saw 0.02 to 0.07 lots; a
2-point channel stop behind a 5-point template stop put ~5% at risk. It now
passes `template_sl_at(template, dir, tick)` to `lot_sizing.template_lot`,
like instant entry and the Reversal Engine's limit orders already did;
`sl_pips = 0` keeps the signal's stop for both. The immediate Telegram path
(`scan_auto_execute`) sends a single-leg template trade with the SIGNAL's stop
and sizes from it, so its lots vary with the channel's stop but its risk is
the configured %; whether that path should place the template's stop instead
is an open question for the owner. Pinned by
`tests/risk/test_template_trade_sized_from_the_stop_it_places.py`.

## Per-channel daily loss cap (2026-09-27)

`services/risk/channel_loss_cap.py`. Once a Telegram channel's net realised
P&L today reaches minus its cap, new automated entries from that channel are
refused until the day turns over; other channels keep trading and open
positions are not touched. Off by default (`channel_daily_loss_cap` = 0);
per-channel overrides live in `channel_daily_loss_caps` (JSON, canonical
names, 0 exempts a channel). Edited on Trading > Schedule; the card's state
rides `/api/schedule/state`, writes go to `PUT /api/channel-loss-cap`.

Load-bearing properties, pinned by `tests/risk/test_channel_daily_loss_cap.py`:

- **Enforced inside `check_trading_schedule`, first, ahead of the schedule's
  master switch.** Every automated route that carries a channel already calls
  that function with the channel's name, so one call covers them all. Turning
  the windows off does not turn the cap off.
- **Counted by CLOSE time, net, broker trades only** (`mt5_ticket IS NOT
  NULL`), since midnight on the trading clock. The daily profit target counts
  by OPEN time; the two predicates differ on purpose (a loss taken today was
  lost today).
- **Names are canonicalised on both sides**: a trade booked under
  `Telegram Auto (X)` counts against `X`, and a gate called with either form
  is the same channel.
- **Engines are not channels**: Reversal, Breakout, Bounce and ORB are
  skipped (`channels.performance.internal_engine_names()`).
- **Failure direction**: an unreadable setting is off; an ARMED cap that
  cannot read today's trades refuses the entry.
- **Sync**: both keys ride the trading schedule snapshot; a snapshot without
  them (an older peer) leaves them alone.

Known limit: the P&L read is this node's own `vantage_simulated_trades`. On a
Mac forwarding to the VPS under centralized signal generation, trades closed
on the VPS are not in the Mac's table, so the Mac's gate under-counts. The
governor's daily loss limit has the same limit.

No "resume for today" button, unlike the daily profit target: raise or clear
the channel's cap instead. Needs a demo session before a cap is set on the
live account.

## The daily goal (2026-09-29)

Risk > Stopping for the day > Daily goal (`risk/daily_goal.py`,
`docs/todo/risk/020`). Once realised P&L since the broker day opened reaches
the goal, the same `trade_pause_until` + `risk_halt_reason` pair the other
daily halts write is set until the next broker day. Open trades run on; only
new entries stop. Owner's choices, 2026-09-29: realised P&L only, new entries
only, every source.

- **Reached with trades open is a HOLD, not the day's halt (2026-09-30).**
  The VPS halted on "+$34.31 vs $32.33" with trades open; they closed at a
  loss and the day ended under the goal, still halted. Now: reached and MT5
  reports positions open (any position on the account; unknown counts as
  open) writes "Daily goal reached (...), waiting for N open trades to close",
  which blocks new entries like any halt. Flat and still at the goal: the
  normal "Daily goal secured" halt. Flat and under it: `lift_hold` clears the
  pause, but first re-runs the governor, give-back and daily-loss checks the
  way `close_trade` does, because while the hold stood those guards saw
  "already paused" and wrote nothing. Lifting needs a live balance and only
  ever lifts a reason starting "Daily goal reached", never another guard's.
- **Measured on MT5's realised figure, not the local table (2026-10-07).**
  The halt summed `vantage_simulated_trades`, which held only the day's four
  small winners (+$39.24) while MT5 had the day at -$41.05: the losing closes
  reached MT5 by another route and never had a local row. It secured a $23.77
  "goal" on a losing day; the real goal was $26.99 (4% of MT5's opening
  balance). The header's "Today's Goal" already read MT5. Now the sweep and
  the breakeven-protect sweep both read `todays_realised.since(bridge,
  window_start)`, the same closed-trade rows the Calendar sums, from the
  goal's own window (so Resume still restarts it). MT5 cannot answer: no
  halt, no lift, no stop moved that sweep; the local table is not a stand-in.
  A **secured** halt now lifts too when flat and MT5 shows the day under the
  goal, so a halt written on a wrong figure, or a day a manual order took
  back under, does not stand all day. A bridge with no `get_deal_history` at
  all (test fakes) still uses the local table.
- **`%` is of the day's OPENING balance** (live balance minus today's
  realised), so the same percentage asks for more as the account grows. That
  is the compounding the owner asked for. `$` is a fixed amount.
- **It is evaluated by the position monitor cycle, not in `record_close`.**
  The daily-loss and give-back halts run inside `record_close`; that function
  is frozen, so this one could not join them. The sweep runs every cycle,
  throttled to `SWEEP_EVERY_S` (5s), outside the open-trades block (the goal
  is usually reached by closing the LAST open trade). **So an entry that
  arrives in the seconds between the close that reaches the goal and the next
  sweep can still open.** Closing that window means reshaping the close path.
- Resume restarts its window: `rearm_risk_guards` writes
  `daily_goal_baseline_ts` beside the other two baselines. The key is spelled
  out in `governor.py` because `daily_goal` imports `governor`.
- **A Resume restarts the count of profit, never a loss (2026-10-07, owner).**
  The day was -$41.05 on MT5, trading was resumed at 09:22, +$36.30 came
  after, and the goal secured "+$36.30 vs $25.34" while the header read
  "$26.99 / -$4.75": the morning's loss fell outside the window. The goal now
  counts `goal_figure(day, window)` = realised since the window opened plus
  any loss earlier in the broker day; profit before a Resume is still not
  counted, so resuming past a reached goal still asks for a fresh one. A `%`
  goal is priced from the whole day's realised (the real opening balance), not
  the window's. `broker_realised` asks MT5 twice after a Resume (window and
  day). Pinned by `tests/risk/test_daily_goal_resume_keeps_loss.py`. The
  header's figure is still the whole day, so after resuming past a reached
  goal it shows more than the halt counts.
- **There are now two daily profit targets.** Trading > Schedule's
  `trading_schedule_daily_target` is dollars only, applies only while the
  trading schedule is on, and refuses at the entry gate rather than pausing.
  They do not interact; whichever is reached first stops the day. The owner
  keeps both on purpose (2026-09-29: "they are slightly different").
- Three columns, migration 55, all in `SYNCED_SETTINGS_KEYS`, so the VPS
  holds the same goal. `daily_goal_enabled` is in `_PROTECTIVE_KEYS`, so a
  change to it is logged with its origin.
- **"Move stops to breakeven once reached" (2026-09-30, migration 56,
  `daily_goal_protect_be`, off by default).** A tickbox beside "Goal in".
  While the goal stands (`daily_goal.goal_standing`: reached, or its hold
  still waiting), `positions/goal_breakeven.py` moves every open position's
  stop to entry plus the safety net's cost estimate (spread, slippage,
  commission; not swap). Owner's choices: entry plus costs, and a trade in
  loss is moved later, once price gives it room, never closed. It only
  tightens, waits for `MIN_GAP` ($1) beyond breakeven because the bridge
  clamps a stop inside the stops level and a clamped stop can land looser,
  runs on the active trader node only, records the local stop only after the
  broker accepted, and retries a rejection after 60s. EA-managed trades are
  modified at the broker directly: the EA's `MoveSl` re-reads the live stop
  and only tightens, so it does not undo it. Synced and in `_PROTECTIVE_KEYS`.
  **The trigger is the same local-table judgement as the halt**, not the MT5
  figure the dashboard shows. **Not yet run on a demo account.**

- **A profit target, once reached, stays reached for the day** (2026-10-05,
  bugs/064, handover 038 option A, provisional). `risk/schedule.py`'s
  `TARGET_REACHED_KEY` latches the daily target and each window's target the
  first time the gate *or the header badge* sees it reached; it expires at
  midnight by construction. Resume is still the only way back in. Raising the
  target above the latched level releases it; lowering it does not. Before
  this, a later loss (once, the guard's own force-close) re-opened trading.
  **Not yet run on a demo account.**
