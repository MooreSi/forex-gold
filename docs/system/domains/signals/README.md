# Signals

**Living file — update when this domain teaches you something.**
Covers: `backend/src/services/signals/`, `channels/`, `telegram/`.

## What it is

The ingestion side of the system. A Telethon-based Telegram reader listens to
configured groups and stores raw messages; a scan pipeline classifies each
message (new signal / edit / instant entry / limit order / logic keyword /
noise), parses it with per-channel deterministic regexes or learned rules,
and writes a signal row. A second layer decides *policy* per channel — trust
scorecards, pause state, learned parsing rules, and an AI recommendation of
which strategy that channel's signals should run. Separately, a Telegram
*bot* (Bot API, not Telethon) sends outbound trade alerts and accepts ~23
commands.

This domain **produces and resolves signal rows; it never places, modifies
or closes an order**. Bridge use here is read-only (`get_tick`).

## Where the code lives

- `services/signals/scan_messages.py` — the whole message-scan pipeline (`ScanCtx`): dedup, logic keywords, instant entry, SL adjustment, parse, staleness, strategy resolution, execution, alerting
- `services/signals/parser.py` — the gold-signal regexes: Format A ("Sell Gold 4520 - 4512"), Format B (`Direction`/`ENTRY`), Format C/GD2 ("XAU USD BUY NOW"), C3 limit-order layout, instant entry, learned-rule application
- `services/signals/scan_parse_classify.py` — channel-format-based classification/parse + DB recording
- `services/signals/scan_edit_reparse.py` — dedup/edit-correction state machine for Telegram edits (same message ID, new text)
- `services/signals/scan_staleness.py` — staleness guard (`max_signal_age_secs`) and per-channel strategy/skip-reason resolution
- `services/signals/resolution.py` — front half of `open_trade_from_signal`: gates + resolving strategy / lot size / stop loss (pure computation, read-only)
- `services/signals/pending_activation.py` — zone-fill watcher that activates pending signals when price re-enters the entry zone
- `services/signals/repo.py`, `tg_repo.py`, `commentary.py` — signal CRUD, `vantage_tg_signals` reads/writes, AI commentary JSON
- `services/channels/strategy_ai.py` — Claude-backed per-channel strategy recommender (30-min Sonnet cycle; per-signal Haiku call)
- `services/channels/rule_generator.py` — auto-generates deterministic regex parsing rules from an approved AI-recovered signal
- `services/channels/repo.py`, `parser_repo.py`, `learned_rules_repo.py`, `unrecognised_repo.py`, `performance.py` — channel config, parser config, learned rules, unrecognised queue, scorecards
- `services/telegram/reader.py` (+ `reader_auth.py`, `reader_listener.py`, `reader_common.py`) — Telethon auth state machine, group listeners, message pipeline
- `services/telegram/bot_loop.py`, `bot_dispatch.py`, `bot_readonly.py`, `bot_infra.py` — getUpdates long-poll loop, command routing (`BotDeps`), read-only/toggle commands, process/env commands
- `services/telegram/alerts.py` — outbound Bot API trade notifications
- `services/telegram/keywords.py`, `keyword_triggers.py` — the six Logic Keyword lexicons and their trigger handlers

## Constraints / must not change

- `signals/` must never place, modify or close an order.
- No broker imports in the `telegram/` package; `keyword_triggers` closes trades only through an *injected* callback.
- The four bot commands that can place/close/restart (`/close`, `/marketbuy`, `/marketsell`, `/restartapp`) arrive as injected callables — their bodies stay on the runtime.
- `ai_fallback_fn`, `queue_unrecognised_fn`, `close_trade_fn`, `find_and_apply_instant_followup_fn`, `is_trading_paused_fn` are *required* injected collaborators with no real defaults — their implementations need context only the caller has.
- `rule_generator.py`: AI-generated content is only regex *pattern strings* stored as JSON — no AI-generated code is ever executed, and a rule is not saved unless it passes a self-consistency check against the human-approved values (tolerance 0.011).
- Logic keyword decisions (confirmed with owner 2026-07-22): CLOSE ALL closes only the triggering channel's own most-recently-opened trade, never every open trade; TP HIT is log/notify only, never moves SL or closes anything.
- `keywords.py` `buy_orders`/`limit_orders` are reference lists only — editing them does *not* change the actual detection regexes in `parser.py`.
- `bot_loop.py`: only one process may long-poll a bot token; the authority check + 409 back-off stops paired Mac/VPS installs kicking each other in a loop. One pooled `httpx` client for the loop lifetime.
- The collaborator binding on `ScanCtx` is asserted by `tests/core/test_scan_messages_relocation.py`.

## Known things & gotchas

- Telegram delivers an *edit* as the same message ID with new text; `scan_edit_reparse.py` can **flatten (close) a real open position** via `close_trade_fn` when an instant-entry edit flips direction.
- The limit-order format ("BUY/SELL LIMITS GOLD @ … AREA") is checked *ahead of* every channel's configured `parser_format`, so it fires for any channel (explicit 2026-07-22 decision).
- Parser tolerances are deliberate: non-breaking spaces, optional colons, en/em-dash/slash/"to" range separators, widened currency capture so `XAU/USD` isn't truncated and wrongly rejected.
- `max_signal_age_secs` (formerly hardcoded 4 min) is an Expert Tunable — older signals are recorded as *historical* and never executed.
- Group IDs are remapped by name in `channels/repo.py` (Telegram-side renames keep the same group_id).
- `bot_offset` is persisted to `app_config` so a restart doesn't re-run commands like `/restartapp`.
- `strategy_ai.py` regime thresholds are XAUUSD-specific and hardcoded in-module (session windows, ADX/ATR bands per strategy).
- `scan_messages.py` still carries one piece of engine coupling: `engine_for_eval`, threaded to `evaluate_signal_strategy`.

## Open questions

- `engine_for_eval` in `scan_messages.py` is named "the one piece of engine coupling this pipeline has left" — visible but not yet removed.
- `bot_readonly.py` docstring notes a dispatcher rewire deferred to a future pass; `bot_dispatch.py` has since landed, so the docstring is stale rather than contradicted.
