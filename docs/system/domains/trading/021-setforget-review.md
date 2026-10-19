# Set & Forget implementation review — 19 October 2026

Owner request: investigate the strategy's silence, research its use by Alex G
and other traders, and implement demonstrable corrections. This review covers
the scanner, AI evidence/validation and Trading → Set & Forget explanation.

## What the public examples actually establish

- **Creator demonstration:** [fxalexg, “This Trading Strategy is Boring, but Made
  Me My First $100,000”](https://www.youtube.com/watch?v=E3lYZsy8nYE), accessed
  through its [attributed transcript](https://youtubetotranscript.com/transcript?current_language_code=en&v=E3lYZsy8nYE)
  because YouTube could not be fetched directly. Higher timeframes provide
  direction; an AOI needs three structural touches, including touches from
  opposite sides. Lower timeframes supply the entry after a pullback to the
  AOI, a structure change and continuation evidence. Stops belong beyond the
  structure that invalidates the idea. This is a sequence of events, not an
  instruction to buy every nearby support band. The demonstrated approach
  involves discretion about significant swings.
- **Student worked example:** [“FXALEXG's Set and Forget Strategy made me
  $10,000!”](https://www.youtube.com/watch?v=vedWPL-foIU), available as an
  [attributed transcript](https://youtubetotranscript.com/transcript?current_language_code=en&v=vedWPL-foIU).
  The narrator uses NZDCAD, aligned Weekly/Daily or Daily/H4 structure,
  repeated AOI tests, then M30/M15 structure change, pullback and engulfing.
  The claimed result is self-reported. The useful evidence is the explained
  entry sequence, not the dollar claim. Different timeframe combinations are
  discretionary variants; they do not demonstrate daily gold opportunities.
- **Student experience:** [December 2024 first-person
  account](https://www.reddit.com/r/Forexstrategy/comments/1hhrx8q/fxalexg_swing_trade_strategy/)
  reports three months of demo practice, at most two trades per week, fixed
  initial exits and successful simulated challenge attempts. There is no
  independently verified broker record in that post.
- **Community checklist:** [AnthonyToday's original TradingView
  publication](https://www.tradingview.com/script/2YLynmgt-Set-Forget-AlexG-Club-Checklist/)
  describes configurable confluences and readiness thresholds. It is an
  author's implementation aid, not an official executable strategy or proof
  of returns. Its publication explicitly offers no profitability guarantee.

Inference: daily chart preparation and finding trades across a watchlist are
different from one or two qualifying trades every day on XAUUSD. Public
success anecdotes cannot substantiate the latter or establish expectancy.
The owner-supplied guide remains the existing domain authority; this change
does not silently substitute a student's timeframe/risk policy for it.

There are still material differences from discretionary examples. This app
uses fixed two-bar fractal swings, one M30 trigger timeframe and XAUUSD only;
the examples select significant structure and sometimes refine an entry on
M15 across several currency pairs. The existing scanner watches both sides,
while the reviewer still requires Weekly/Daily alignment, so a counter-trend
candidate can correctly receive a veto. The research does not justify removing
that filter or weakening the R:R floor to force a daily trade. Public accounts
do not isolate which of discretionary selection, execution, risk discipline
or selective reporting explains their claimed success.

## Evidence from this installation

Read-only inspection of October app logs found **1,513 no-setup decisions,
212 AI declines, one refusal, one failure and no recorded placement**. This is
what these retained files record, not an assertion about an entire account.
On 18 October the same price, 4165.65, was repeatedly sent for review during
the closed market. The AI repeatedly identified bearish/ranging higher
timeframes and a buy already inside 4157.77–4187.73 supply, with a target at
4330.48. These were not missed profitable trades: several objections were
real, while other prompt rules contradicted the scanner's implemented policy.

## Defects corrected

| Defect | Correction |
|---|---|
| `gather` removed forming M30; `confirmation` then removed the newest closed bar again. Engulfing arrived a bar late, unlike structure shifts. | Explicit closed-series argument; the live scanner reads the latest closed M30. Existing H4/live-series callers retain their contract. |
| Arrival was checked at current price only. A wick could test the AOI and confirmation close outside the arrival band. | Associate a real M30 wick visit with a subsequent fresh confirmation. Bound the visit to eight M30 bars, departure to one H4 ATR, and reject intervening closes through the distal stop boundary. No touch, stale visit or distant chase cannot activate this added path. |
| A relevant M30 rejection could inherit an unrelated H4 pattern's tail and an unnecessarily wide stop. | An associated reaction carries its wick extremes; stop remains beyond both the AOI and the reaction, with the existing buffer. The existing immediate-arrival trigger path is retained. |
| Cross-timeframe overlap chains could make a valid zone exceed the width rail, then disappear from proposal construction. | Cap the combined width at the existing rail, keeping separate levels when joining them would erase usable structure. Three-touch validation is unchanged. |
| Opposing supply/demand containing the market entry was skipped in favour of a distant target. | Such a band is now an obstruction. Target geometry/R:R refuse the trade instead of manufacturing room. |
| AI rules still prohibited the scanner's existing 3R no-opposing-zone fallback and described blind resting entries; no stage or M30 evidence was supplied. | Prompt reflects the implemented entry stages, bounded visit and fallback; rendering includes trigger, visited AOI, structural stop and target basis. |
| AI adjustments could move a stop inside invalidation, skip the next obstruction, change a market entry within the large ATR tolerance to inflate R:R, or contain non-finite prices. | Reject these adjustments; preserve the measured stop boundary, opposing target, actual market entry and finite prices. Retain the rules' proposal and explain the rejection. |
| A weekend signal could repeatedly reach billable AI review. | Live gathering records the shared market-closed state; scanning returns a wait before review. No order-path code changes. |
| An armed plan was labelled as a resting order. | The UI distinguishes watching from an executable plan, names the confirming event, shows its close time and the target basis. |

Unchanged: 1:2 minimum, 3R fallback, three touches, 0.5 H4 ATR immediate
arrival, 0.25 ATR stop buffer, both-direction scanning, existing AI veto,
Auto's demo-only restriction, trade caps, shared placement and sizing code.
Eight bars means one H4 observation window, not an optimisation against P&L.

## Reproducible before/after check

`python -m tools.setforget_replay` reads saved D1/H4/M30 JSON and calls the
production `evidence_from`/`scan`/`invalidations`. It reconstructs incomplete
H4/D1 candles from M30 so future highs/lows/closes do not enter the decision.
At a timeframe boundary it includes the new, flat forming candle, matching
the live-series convention. Two tests pin those look-ahead boundaries.

Data: read-only broker candle export, 2023 onward for warm-up. Evaluation
window: **1 July through 18 October 2026**, 3,575 M30 closes. Range exports
were converted back to the app's broker clock before aggregation. The baseline
uses the pre-change backend from Git HEAD and the identical replay driver,
clock conversion and input files. No threshold search or AI calls.

| Measure | Before | Corrected |
|---|---:|---:|
| Closes with an M30 trigger | 816 | 958 |
| Closes passing deterministic placement checks | 229 | 147 |
| UTC dates with at least one eligible close | 60 | 37 |
| Entries, one position at a time | 30 | 22 |
| Closed trades | 29 | 22 |
| Winning closed trades | 6 | 5 |
| Net realised R | -7.21 | -3.89 |
| Mean realised R | -0.249 | -0.177 |

Multiple closes may describe the same opportunity. Dates include Sunday
market openings. The reduced eligible count is substantially the removal of
entries already in opposing zones, not a new frequency target. The method
does find executable candidates; the replay does **not** establish a positive
edge or explain whether the AI improves realised returns.

Simulation limitations: one position at a time, M30 stop-first ordering when
SL and TP share a candle, fixed $0.30 price cost, no slippage, gap or financing
model, no real-time latency and no discretionary/AI filtering. The baseline
still had one open trade at the window's end. This is a correctness and
diagnostic comparison, not audited performance or live validation.

An earlier, disjoint period was checked with the same frozen parameters:
**19 October 2025 through 30 June 2026**, 8,195 M30 closes. Baseline: 518
eligible closes, 43 entries, 42 closed (10 wins), **+19.43R**, one still open.
Corrected: 413 eligible closes, 67 entries/closed trades (12 wins), **-10.11R**.
This is a material deterioration in that period. Correcting candle timing,
stops and fictitious target room changes which trades occur and how long a
position occupies the one-position slot; fixing correctness does not imply
better historical returns. Taken with the recent negative period, the
corrected mechanical rules do not currently establish a profitable edge.
No numerical values were retuned after observing either result.

Saved inputs, SHA-256 manifest and both result periods are retained privately
in `DATA_DIR/setforget_review_2026-10-19/`. Baseline source commit:
`8fedd92f931d08c7b5a9f8bb8c3e0de80a3fd525`. The replay tool belongs to the
source tree; broker exports and runtime logs are not added to the public repo.

## Validation and operational boundary

The regression pack covers latest-closed versus forming engulfing, BUY/SELL
zone reactions, absent/stale touches, chasing, a failed visit, rejection tails,
bounded merging, prompt evidence, invalid AI stops/targets/market entries and
non-finite prices, an opposing zone containing entry and closed-market scans.
All pre-existing tests stay intact. Final focused backend/API and frozen
close-path checks: **400 passed**. Full frontend: **1,613 passed**, with the
production bundle rebuilt. Architecture/smoke/doc gates pass.

Full repository backend run with coverage: **13,393 passed, 20 existing
skips** (13,413 collected, 11m34s). Coverage ratchet: **no regressions**.
All nine architecture/import/structure/facade/orphan/async/boot/doc checks pass.
The earlier broad runs were interrupted as additional review regressions were
added; these final counts come from the completed run on the final backend.

The running app and paired trading node need their normal update/restart to
load the corrections. This session does not restart a trader carrying other
uncommitted work, change the account mode, enable Auto or place a demo/live
trade. Forward demo observation is still required to measure actual approved
fills and the AI filter's effect. Automatic swing detection remains an
approximation of the discretionary examples; no paid-course parity is claimed.
