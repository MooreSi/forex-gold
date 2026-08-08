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

- **Per-source schedule toggles (2026-07-24):** each of the 7×3 windows independently gates Telegram / Reversal Engine / Breakout Engine, because Reversal Engine performs well overnight (Asia) but loses during London/NY — the opposite of the Telegram channels.
- Profit-per-window is computed **on demand** — `SUM(net_pnl)` of closed trades whose `open_time` falls in today's window — so it can't drift and needs no midnight reset.
- Two clocks in one domain: schedule times are local wall-clock HH:MM (matching the UI), while `is_session_allowed` maps sessions in **UTC** (Asia 21–07, London 07–12, overlap 12–16, NY 16–21).
- `RR_BYPASS_SOURCES` matching is case-insensitive and substring-based — the R:R floor is skipped entirely for paid-provider channels, including wrapped names like "Telegram Auto (Gold Diggers VIP)".
- `price_in_entry_range` is asymmetric on purpose: BUY zones are pullback areas, SELL zones are rally areas — the opposite side means chasing price.
- `is_session_allowed` imports `dpm.engine` *locally* to avoid a circular import — the session gate transitively depends on the DPM engine.
- Risk settings are served from a 10s TTL cache living on the `database` module itself; `update_risk_settings` carries a re-entrancy guard for sync-applied changes.
- `retention.switch_environment` is the genuinely dangerous call in that module: `db.init()` closes stale connections and flushes every registered cache, and the whole app then reads a different file.

## Open questions

- The Expert Tunables clamp ranges are "documented guesses, flagged for review" — the bounds themselves are unvalidated.
