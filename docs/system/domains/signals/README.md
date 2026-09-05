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
- `services/signals/scan_parse_classify.py` — one channel-agnostic classification/parse pipeline + DB recording
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
- `keywords.py` `limit_orders` is a reference list only — it feeds the AI-fallback gate and nothing else; editing it does *not* change the limit-order regex in `parser.py`. **`buy_orders`/`sell_orders` are different since 2026-08-27**: they are live bare-direction triggers (see Known things).
- `bot_loop.py`: only one process may long-poll a bot token; the authority check + 409 back-off stops paired Mac/VPS installs kicking each other in a loop. One pooled `httpx` client for the loop lifetime.
- The collaborator binding on `ScanCtx` is asserted by `tests/core/test_scan_messages_relocation.py`.

## Known things & gotchas

- **A terminal branch that records nothing is re-run forever, and "just record it" is not automatically the fix (2026-09-05, bugs/015).** The bare-direction branch of `classify_and_parse` was the one terminal path recording nothing, so the scan loop re-parsed the same message about once a second for as long as it stayed in the reader's fetch window -- 8,319 times for one 15-character SELL. The obvious fix, a parked `vantage_tg_signals` row, is wrong twice: the follow-up matcher reads `vantage_second_message_holds`, never that table, so the row helps it not at all; and `scan_messages.py`'s dedup probe routes any message that HAS a row into `_handle_signal_edit_impl`, where an edit adding full levels executes **only** when the status is `pending_followup` -- so a row parked under any other status turns a taken trade into a missed one. **Check what a row's mere existence changes before adding one to this table.** What shipped instead is an in-process, read-only guard keyed on the id AND a digest of the body, sitting below the learned-rules parser and the second-message block. Keyed on the body because a Telegram edit keeps the id and changes the text, which is how one of these becomes a real signal; read-only because a probe that records would suppress the first sighting's own log line. Whether these messages should be recorded at all is `docs/simon-handover/027`. Pinned by `tests/core/test_bare_direction_rescan_work.py`.
- Telegram delivers an *edit* as the same message ID with new text; `scan_edit_reparse.py` can **flatten (close) a real open position** via `close_trade_fn` when an instant-entry edit flips direction.
- **Parsing is channel-agnostic (2026-08-27, owner directive).** `classify_and_parse` no longer branches on the channel's configured `parser_format` at all: every channel runs learned rules -> second-message merge -> limit-order layout -> currency guard -> Format A/B -> GD2 -> partial -> bare trigger -> AI fallback -> unrecognised queue, in that order. Before this, a `format_ab` channel never ran a single GD2 regex and a `gd2` channel never ran Format A/B, so a well-formed signal in the "wrong" layout for its channel was dropped. `parser_format` still does two things and only two: `'none'`/disabled stops the channel being scanned, and it sets the DEFAULT for that channel's Immediate Market Entry flag.
- **Immediate Market Entry is channel-agnostic too (same change).** The scan path used to pick the trigger parser from `parser_format`, and the non-gd2 one requires a literal `XAU… BUY NOW`. So on Gold Diggers VIP a market entry worded "Buy Gold Now" / "Buy Zone Now" / "XAU USD BUY" matched nothing at all, while the identical message on a gd2 channel fired. Both trigger parsers now run for every channel, with no substring pre-gate.
- The limit-order format ("BUY/SELL LIMITS GOLD @ … AREA") is checked *ahead of* every parser, so it fires for any channel (explicit 2026-07-22 decision). **It is still the only thing that places a genuine resting broker order** — every other zone signal is a Python-side wait that fills at market. Whether that should change is Simon's call: [008-zone-signals-and-real-pending-orders.md](../../../simon-handover/008-zone-signals-and-real-pending-orders.md).
- **A signal row's `source_name` is decorated** — `Telegram Auto (<channel>)` — while `channel_performance` / `channel_strategy_rec` / `channel_parser_config` are keyed on the bare name. `channels/repo._canonical()` strips that wrapper generically since 2026-08-27; before that it only knew the two decorated strings hardcoded in `CANONICAL_CHANNELS`, so any third channel's Channel Strategy pick was silently ignored everywhere a stored signal was read — expiry ladder, grid dispatch, `open_trade_from_signal`'s own strategy resolution, per-channel schedule windows.
- **The pending watcher's momentum check reads a cache nothing cleared.** `runtime._dpm_candles` was refreshed only while a trade was open, so once the last position closed it froze at that bar — and the watcher defers any signal whose direction disagrees with the last candle it is handed. A frozen bearish bar deferred every queued BUY until it expired. `monitor_cycle` now refreshes it whenever the watcher will read it, and clears it on a failed fetch.
- **That momentum check only runs at all when DPM is enabled**, because the candle cache is DPM's. With DPM off it is silently skipped. Left as-is — turning it on would decline *more* trades, the opposite of what was reported. See Open questions.
- **The BUY/SELL Orders boxes are live triggers (2026-08-27, reported live).** GOLD DIGGERS INSTITUTIONAL sent "PREPARE FOR A BUY" with that exact phrase saved in the BUY Orders box and the app did nothing — it could not have. `buy_orders` fed exactly one thing, the allow-gate in front of the AI fallback, so a phrase typed into it bought permission to spend an AI call and nothing else; direction detection lived entirely in `parser.py`'s per-format regexes. There was no `sell_orders` box at all, so the feature could only ever have worked one way. Both boxes now drive `keyword_triggers.parse_lexicon_direction_trigger`, which returns the same `(direction, None)` shape as the built-in bare triggers and takes the same path: market entry when IME is on for that channel, held quietly otherwise. Every channel, like every Logic Keyword.
- **That trigger matches per-LINE and exactly — not as a substring**, unlike every other lexicon here, and it refuses any message containing a digit. These phrases become market orders: as substrings they would fire on most of what a gold channel says in a day, and the no-digit rule keeps anything that states a level with the per-format parsers, which will use those levels rather than ignoring them. A message naming both directions is refused rather than guessed at.
- **No shipped default is a single bare word** (owner directive, after `BUY`/`SELL` shipped briefly). Line-exact matching stops a one-word phrase firing on a *sentence*, but not on a channel posting a lone `BUY` as commentary — which is not distinguishable from one posting it as an instruction. Every default names the pair, the zone or the action. A bare word is still addable in the UI; it just cannot arrive by default. Pinned by `test_no_shipped_default_is_a_single_bare_word`.
- `DEFAULT_LEXICONS` is only the fallback for a category with no saved row. **Editing a default does not change an install that has already saved that box** — and the Parsing tab's Save writes every box at once, so any install that has ever pressed Save has its own copy of all of them.
- `keyword_triggers.AI_FALLBACK_GATE_LEXICONS` names the lexicons the AI-fallback gate combines. Two tests had gone stale enumerating three of them by hand; derive from that constant instead.
- The Logic Keywords boxes on the Parsing tab are rendered from `DEFAULT_LEXICONS`, not a hand-written tuple — a box missing from the UI is now a *trigger nobody can turn off*, not just an uneditable list. Pinned by `tests/frontend/test_parsing_settings_render.py`.
- Parser tolerances are deliberate: non-breaking spaces, optional colons, en/em-dash/slash/"to" range separators, widened currency capture so `XAU/USD` isn't truncated and wrongly rejected.
- `max_signal_age_secs` (formerly hardcoded 4 min) is an Expert Tunable — older signals are recorded as *historical* and never executed.
- Group IDs are remapped by name in `channels/repo.py` (Telegram-side renames keep the same group_id).
- `bot_offset` is persisted to `app_config` so a restart doesn't re-run commands like `/restartapp`.
- `strategy_ai.py` regime thresholds are XAUUSD-specific and hardcoded in-module (session windows, ADX/ATR bands per strategy).
- `scan_messages.py` still carries one piece of engine coupling: `engine_for_eval`, threaded to `evaluate_signal_strategy`.

## Open questions

- The pending watcher's momentum confirmation is coupled to the DPM toggle by accident: it reads DPM's M5 candle cache, so with DPM off the gate never fires. Should it have its own fetch, or is it meant to be a DPM-only gate?
- Should a zone signal rest as a real broker order instead of waiting in Python? See [008-zone-signals-and-real-pending-orders.md](../../../simon-handover/008-zone-signals-and-real-pending-orders.md).

- `engine_for_eval` in `scan_messages.py` is named "the one piece of engine coupling this pipeline has left" — visible but not yet removed.
- `bot_readonly.py` docstring notes a dispatcher rewire deferred to a future pass; `bot_dispatch.py` has since landed, so the docstring is stale rather than contradicted.
