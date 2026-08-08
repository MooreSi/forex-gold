# Analytics

**Living file — update when this domain teaches you something.**
Covers: `backend/src/services/analytics/`, `ai/`, `notifications/`.

## What it is

The read side of the app: trade-history and performance queries, per-engine
edge statistics, the ORB/IVB pre-London breakout report, LLM-generated
market and trade commentary, and the email digests that ship those numbers
out. `analytics/` computes; `ai/` asks an LLM (Claude or DeepSeek) for
commentary and recovers signals the deterministic parsers missed;
`notifications/` builds and sends the daily/weekly/ORB emails and owns the
email + Telegram notification config.

## Where the code lives

- `services/analytics/trade_history_repo.py` — SELECT-only queries backing the history views; returns raw rows, not dicts, on purpose
- `services/analytics/read_repo.py` / `pnl.py` — hourly P&L grid, realised 24h P&L, equity drawdown, regime score, signal execution lags
- `services/analytics/ticket_maps.py` — per-ticket lookups merging the cross-node ledger with local rows
- `services/analytics/labels.py`, `formatting.py` — display names, broker-timestamp/duration formatting
- `services/analytics/edge_stats.py` — per-engine edge stats read live from each engine's own DB via `mode=ro` URIs
- `services/analytics/orb_report.py` — ORB/IVB range detection, volume profile, backtested target multiple
- `services/analytics/reporting.py`, `ai_analysis.py`, `signal_lab_repo.py` — performance computation, the AI Trade Analysis page's cross-DB gathers, read-only access to `test_signal.db`
- `services/ai/provider.py` — unified `complete()` over Anthropic/DeepSeek, selected by `cfg["ai_provider"]`
- `services/ai/claude_ai.py` — per-use-case prompts, JSON schemas and fallback dicts
- `services/ai/signal_extractor.py` — LLM fallback signal extraction after regex parsers fail
- `services/ai/model_refresh_loop.py`, `commentary_repo.py`, `recovered_repo.py` — daily model-catalogue refresh, persistence
- `services/notifications/email_service.py` — transport only (SMTP / Resend / Mailjet)
- `services/notifications/email_html.py`, `scheduler.py`, `config.py`, `repo.py` — HTML presentation, the per-cycle scheduler sweep, config

## Constraints / must not change

- `analytics/` is **SELECT-only by contract**. Anything that writes, or can reach the broker, belongs in another service. (`orb_auto_execute` lives in trading for exactly this reason.)
- `ai/` touches no broker: the AI-fallback path that makes a real `modify_order` call lives in trading, not here.
- `edge_stats.py` and `signal_lab_repo.py` open SQLite with `mode=ro` URIs — read-only by construction, not convention.
- `ticket_maps.py` merge precedence is behaviour: ledger first, local second, so local rows overwrite ledger rows. Reversing it degrades every locally-opened trade to the peer's summary.
- The `open_time >= cutoff` predicate in `trade_history_repo.py` must stay — dropping it turns a bounded scan into a full-table scan on a forever-growing DB.
- `formatting.format_broker_ts` deliberately interprets MT5's UTC+3-encoded epoch naively (yields broker time for display); `broker_ts_to_uk_date` subtracts the offset first.

## Known things & gotchas

- One deliberate exception to no-writes: `get_orb_target_multiple` memoises its backtested multiple into `app_config` so the 25-day backtest runs once per day.
- `signal_lab_repo.py` exists as a named adapter because `db_module.db()` resolves to whatever `init()` last pointed at — folding those queries into the main repo would silently read the *trading* DB.
- The `except Exception: pass` around each source in `ticket_maps.py` is deliberate swallow-and-degrade — one missing source must not blank the others.
- `edge_stats.py` returns empty results for a missing engine DB (engines are optional installs); its profit-factor clause keys on `outcome IN ('win','loss','be')` to exclude open trades.
- `model_refresh_loop.is_running` is a **callable**, not a bool — a captured value would leave the loop spinning after shutdown.
- `scheduler.py`'s ORB time gate uses `Europe/London` while the daily/weekly gate uses bare server-local `datetime.now()` — recorded as pre-existing and preserved.
- `signal_extractor.extract_signal()` runs only after a deterministic parser fails, and only yields a trade with model confidence AND complete real price levels.

## Open questions

- The scheduler London-vs-server-local timezone inconsistency is preserved, not resolved.
- The `core_orb_report` split is still pending (auto-execute moves with the trading surface).
