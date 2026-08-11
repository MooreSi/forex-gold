# CLAUDE.md — the lab (notebooks/)

## Project

An isolated data-science lab for `c:\dev\forex\app` (a live-money MT5
gold-trading app). The lab replays historical signal data from read-only
SQLite snapshots to test strategy ideas — filters, geometry, ML — before
anything touches the real system. Python 3.11, pandas/numpy/sklearn, plain
`.py` files with `# %%` cells (never `.ipynb`). Primary users: Simon
(non-technical — plain-language READMEs) and AI agents.

## Non-negotiables

1. **Never import from `app/`** (`backend`, `frontend`, anything). Copy the
   few lines you need and cite the source path in a comment. The reverse
   also holds: never make `app/` import from here. *(advisory — no gate)*
2. **Never connect to MT5, Telegram, or any broker/network API** from lab
   code. The lab reads `_shared/data/*.db` and local CSVs only.
3. **Never read `mt5_credentials`, `telegram_config`, or `email_config`**
   tables. *(enforced: `loaders._read()` raises `PermissionError`; DB
   connections are opened `mode=ro`.)*
4. **Never edit or delete files in `_shared/data/`.** New snapshots get
   dated filenames (`reversal_engine_2026-08-18.db`); old ones stay.
5. **One simulator.** All replay goes through `_shared/lib/sim.py`.
   Experiments configure it via `SimConfig` hooks; never fork or copy it.
   If it's wrong, fix it and re-run 001 to re-calibrate.
6. **Never split train/test randomly.** Day-based splits only
   (`_shared/lib/splits.py`). Signals minutes apart share the same market
   move; random splits leak.
7. **Report every result with the full `metrics.summarize()` suite**,
   including `trades` vs `candidates` — a config that wins by deleting 95%
   of trades must be visible as such.
8. **Never promote a finding into `app/` yourself.** Promotion is a
   separate, user-requested task under `app/`'s own CLAUDE.md rules (specs,
   tests, Simon sign-off on money behaviour).

## Experiment conventions

- One folder = one question: `NNN-short-slug/` with `run.py`, `README.md`,
  `output/`. Next number = highest existing + 1. Never rename or delete an
  existing experiment — negative results are assets.
- `README.md` has exactly: **Hypothesis / Data used / Method / Result** (and
  optionally Interpretation/Verdict). Fill in Result the same session the
  numbers are produced; date it.
- `run.py` must run top-to-bottom non-interactively:
  `python NNN-slug/run.py` from `notebooks/`. It prints its findings and
  writes CSVs to its `output/`. No `input()`, no hardcoded absolute paths —
  derive paths from `Path(__file__)` (see 001 for the pattern).
- All P&L in **R-multiples** (risk units, full stop ≈ −1R ≈ $50). Dollars
  are presentation only. Sizing in the data is risk-based, so dollars
  mislead across strategies.
- State the price source in every README: "60s series" (directional only)
  or "M1 candles" (testable). Ideas that survive the 60s series get re-run
  on M1 before being called real.

## Data gotchas (verified against the 2026-07-21→31 snapshots)

- `re_signals.stop_loss` is the **final** stop (post break-even/trail), not
  the original — 359/741 rows have it on the profit side. Original stop =
  `zone_mid ∓ sl_dist`. `sim.py` reconstructs this; anything else reading
  `stop_loss` must too.
- Timestamps: `reversal_engine.db` uses epoch floats;
  `forex_trader_demo.db` mixes epoch ints (`consolidated_trades`) and ISO
  strings (`telegram_messages`). Use `loaders.py`, which normalises.
- `consolidated_trades` contains probable duplicate rows (sync echoes,
  identical P&L seconds apart) — use `loaders.dedup_trades()` and say so.
- `channel_performance` disagrees with the real ledgers (shows RE +$708
  while both P&L ledgers show heavy losses) — never source performance
  claims from it.
- The ~60s price series (`loaders.prices_df()`) has overnight/weekend gaps
  and can't see intra-minute wicks; `sim.py` resolves same-bar TP+SL as SL
  (pessimistic) and a gap across the entry zone as no-fill.
- The whole dataset is 9 trading days in one strongly-trending gold market.
  Say "on this sample" in every conclusion; re-run when new snapshots land.

## Definition of done, per experiment

1. `python NNN-slug/run.py` exits 0 and reproduces the numbers in its README.
2. README Result section filled in, dated, with the metric suite and the
   walk-forward (not just in-sample) numbers.
3. The lab index table in `README.md` (Simon's file) gets one new row in
   plain English.
4. If the experiment found a data gotcha, add it to this file's list above.

## Communication

- Simon-facing text (READMEs, index table): plain English, no jargon, no
  acronyms without expansion. Agent-facing text (this file, code comments):
  terse and technical.
- Report negative results as plainly as positive ones. "No edge, here's the
  table" is a valid, complete answer.
- Quote R first, dollars in parentheses. Always give trade counts.
