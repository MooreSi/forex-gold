# Set & Forget — the Alex G swing method

**Living file — update when this domain teaches you something.**
Covers `backend/src/services/setforget/`, `backend/src/controllers/setforget_controller.py`,
`backend/src/api/routers/setforget.py` and `frontend/src/components/setforget/`.

Added 2026-09-19 at the owner's request.

## What it is

A section of the Trading tab that reads gold top-down the way Alex G
(fxalexg / Swing Trading Lab) teaches it, proposes one trade, optionally has the
configured AI review that proposal, and hands it to the operator to place.

**It is not an engine.** Nothing here runs unattended, nothing polls for
setups on its own behalf, and there is no scheduler. Every order starts with a
person reading the card and pressing a button.

## Where the code lives

| Concern | File |
|---|---|
| Swing points, bias, last impulse | `services/setforget/structure.py` |
| Engulfing bars and pin bars | `services/setforget/patterns.py` |
| Areas of interest | `services/setforget/aoi.py` |
| Weekly candles from daily ones | `services/setforget/resample.py` |
| The scored checklist | `services/setforget/confluence.py` |
| A candidate, its ratio, its refusals | `services/setforget/setup.py` |
| Orchestration and the AI review | `services/setforget/analysis.py` |
| What the model is told | `services/setforget/prompt.py` |
| The three endpoints | `api/routers/setforget.py` |
| Chart overlay geometry | `frontend/src/components/setforget/hooks/useChartGeometry.ts` |
| Chart colours (shared) | `frontend/src/components/shared/chartTheme.ts` |
| The section | `frontend/src/components/setforget/` |

## The source of record (2026-09-21)

The owner supplied the 36-page guide `Forex Swing Mastery` (Scribd 941847529)
as plain text. **That document is now the authority for this domain**, above
the third-party write-ups the first implementation was built from. Where the
two disagree, the guide wins and this file says which changed.

Two other sources are worth keeping, and both are quoted rather than
paraphrased because neither is the guide:

- The community's [Perfect Checklist](https://www.tradingview.com/script/2YLynmgt-Set-Forget-AlexG-Club-Checklist/),
  an open-source Pine indicator whose source carries the exact confluence
  weights: Weekly 55%, Daily 55%, 4H 45%, and a fourth group labelled
  **"2H, 1H, 30m"** worth 25% holding Round Psychological Level (5), Shift of
  Structure (10) and Engulfing Pattern (10). Nothing in it scores Fibonacci or
  RSI — both of which this app scores, on weaker sourcing.
- [Revelio Trading's mechanical rebuild](https://www.youtube.com/watch?v=nFIJ0z8_01w):
  16 pairs, 10 years, ~9M candles, dev/test split. Records Alex G's own claim
  as 60-65% at **1:4** (the guide says 2:1 minimum, 3:1 preferred), and states
  that the faithful version **struggled in the backtest** — the follow-up had
  to ablate rules to find anything that held out-of-sample. Its chapter list
  also names **AOI expiration** and **AOI width** as parameters that mattered.

## What the guide settled, and what it changed

| Rule | Quote | Status |
|---|---|---|
| Zones drawn from candle **bodies** | "focusing on candle bodies (not extended wicks)"; "using candle-body closes — ignoring long wicks" | **Changed.** `aoi.zones` drew wick-to-body and now spans the body. |
| Zone width kept narrow | "aim for ~<60 pips" | **Added** as `aoi.MAX_ZONE_WIDTH_PCT`, applied in `propose`. |
| Inside bar and doji are patterns | "Inside Bar... Doji" | **Added** to `patterns`, both directionless. |
| Three touches validate an AOI | "touched by price at least three times" | Already in (owner's rule, same day). |
| AOIs on Daily and Weekly | "identify AOIs on Daily and Weekly charts" | Already in. |
| Two higher timeframes must agree | "at least two higher timeframes agree" | Already in. |
| Stop beyond the wick | "just below the pin bar's tail" | Already in — and now deliberately DIFFERENT from the zone rule above. |
| Target at the next AOI, min 2:1, prefer 3:1 | three separate passages | Already in. |
| Risk 1-2% | "1–2% for live accounts" | Uses the app's own risk setting. |
| Entry trigger on 30m | "a 4:1 or 8:1 ratio (e.g. Daily, 4H, 30-min)" | **Built** 2026-09-21 on the owner's confirmation — see below. |
| 1-2 trades per week | "aiming for only 1–2 trades per week (at most)" | **NOT built.** |
| London/NY overlap, avoid Asian | "avoids the thin liquidity of the Asian (Tokyo) session" | **NOT built.** |
| EMA 21 | "price is above the 21 EMA" | **NOT changed** — the guide says "e.g.", so it is the owner's call. |

**Bodies for zones, wicks for stops.** The guide is explicit both ways and they
are different questions: bodies say where most traders closed, so they decide
where the LEVEL is; the wick says where the idea is wrong, so it decides where
the TRADE is. Drawing zones from the wick made every band as tall as the
rejection that formed it, and a long tail — exactly what marks a good level —
produced the widest, most swallowing zones.

Every open question is in
[docs/simon-handover/set-and-forget-open-questions.md](../../../simon-handover/set-and-forget-open-questions.md).

## The rules it implements

Reconstructed from Alex G's free material, the G-Club community checklist
indicator and third-party write-ups. **The full course is paid and none of it
is reproduced here.** Where the public record is thin the code says so.

1. Top-down over Weekly / Daily / 4H. Weekly and Daily must agree or there is
   no trade.
2. Structure is HH/HL or LL/LH. Anything else is a range and is skipped.
3. Entries only at an Area of Interest, in the direction of the higher-timeframe
   bias.
4. The stop is structural: beyond the zone, or beyond the confirmation candle's
   tail when one has closed. `analysis._stop_for` takes whichever is further
   out, so the stop is outside both.
5. The target is the next opposing zone. It is **not** a multiple of the risk —
   when there is no opposing zone there is no candidate, because inventing a
   target would invent the ratio too.
6. Minimum 1:2. `setup.MIN_RR`, enforced in `setup.invalidations`.
7. Risk 1-2% of the account. Sizing forwards to `fees_sizing.suggest_lot_size`.

The EMA 50/200, Fibonacci-band and RSI items are the **community's confluence
list, not a published rule of his.** They are scored as supporting evidence and
are never the reason a candidate exists. Keep it that way: the two items that
decide whether there is a trade at all are `htf_agreement` and `at_aoi`, and
they carry two points each precisely so four weak confirmations cannot outvote
them.

## Constraints that are not obvious

**There is no W1 in this app.** `mt5_bridge._TF_MAP` stops at D1, and so does
the fake market's `TF_SECONDS`. The weekly series is aggregated from the
dailies in `resample.to_weekly`. Do not "fix" this by asking the bridge for
`W1` — it returns an empty list, and the weekly bias then reads `unknown`
forever with nothing on screen looking wrong.

**Week boundaries are computed on the real instant, not the MT5 stamp.** MT5
encodes server time (UTC+3) as if it were a UTC epoch. Gold opens around 22:00
UTC on Sunday; bucketing the raw stamp by ISO week puts that opening bar in the
week that just ended, so the newest weekly bar is built from one candle. Weeks
run Sunday 00:00 UTC to Saturday 23:59 UTC. Pinned by
`tests/services/setforget/test_resample.py`.

**Swing detection breaks ties to the earlier bar.** A bar that tops out and the
bar opening at its close often share a high to the tick, and gold quotes to two
decimals. The rule is "strictly higher than everything before it in the window,
at least as high as everything after". Without it one series gives two swings
where a trader sees one.

**An area of interest needs THREE touches** (`aoi.MIN_TOUCHES`). The owner's
rule, 2026-09-21: scan backward on the higher timeframes until the most recent
three touches of a key horizontal zone validate an AOI; once those majors are
marked, execution works only on how current price interacts with them.

**The scan stops as soon as it has the pair** (`aoi.mark`). It grows a window
from the most recent bar until there is a validated demand at or below price
AND a validated supply at or above it — the same pair a setup needs in either
direction — and then stops, returning `(zones, bars_scanned)`. Growing the
window is what implements "the MOST RECENT three touches": a band's touch count
accumulates as the window reaches further back, so a level validates exactly
when the window first contains its third touch.

**Only the nearest validated band on each side survives**, even when the scan
never completes a pair and runs to the end of the series. Without that trim it
returns everything it passed on the way, which is the endless lookback the rule
exists to stop — measured live on 2026-09-21, the weekly had no validated
supply above price at all, ran its full 82 bars and handed back five levels,
two of them from July and August 2025.

**Levels are marked on the Daily and the Weekly only. The 4H never marks one.**
The 4H is execution: it reacts to the majors, it does not create them. Weekly
and Daily are scanned separately and folded together on overlap, because a
weekly band and a daily band at the same price are one level a trader draws
once. Pinned by
`test_analysis.py::TestTheAreasOfInterestAreMarkedNotMerged`, because `aoi.mark`
could be perfect and `gather` could still hand it the 4H series.

**There is no ATR anywhere in the marking.** Bands are one level when they
OVERLAP. Until 2026-09-21 two bands within `1.5 × ATR` were merged, because
every swing point was a zone and the raw output was a wall of hairlines. That
smear is what manufactured the touch counts the validation now reads, and it
folded a trending instrument's whole range into one band. The wall of hairlines
is now handled by refusing to call a one-touch band a level at all — a filter
rather than a smear. `ZONE_MERGE_ATR` and
`test_analysis.py::TestTheZoneMergeGapIsWired` are both gone; the tests were
not edited to pass, the specification they encoded no longer exists.

**The free read applies the rules too.** Also found by looking. `propose`
builds the best candidate the zones allow; whether it is TRADEABLE is
`setup.invalidations`, and `GET ""` returned an empty list unconditionally at
first — so a 1:0.05 setup arrived on screen looking exactly as tidy as a 1:3
one with Execute enabled. Pinned by
`test_setforget.py::TestTheRulesAreAppliedOnTheFreeReadToo`.

**The Fibonacci band is priced by the backend, not the browser.**
`confluence.retracement_price` is the exact inverse of `confluence.retracement`,
which is what the checklist scores the pullback with, and
`confluence.LEVELS[0]`/`[-1]` ARE `FIB_LOW`/`FIB_HIGH`. So the shaded band on
the chart is the band the checklist scores — not a lookalike. A TypeScript
re-derivation would be a second answer to where 61.8% is, visible as a band
that disagrees with the percentage printed beside it.

**A zone is never zero-height.** A swing candle with no rejection wick would
otherwise produce `low == high`; price is then never "in" it, the checklist
never scores, and the page shows an untradeable level that looks entirely
normal. `aoi.zones` falls back to the candle's body.

**`setforget_lot_size` is deliberately NOT in `sync/server.py`'s synced key
list**, unlike `orb_lot_size`. ORB has an unattended scheduler that reads the
value on whichever node is trading, so the value has to reach that node. This
section has no unattended path: the lot travels with the order.

## The three stages (2026-09-21)

The owner confirmed the entry trigger belongs on the 30-minute chart, so the
entry model was rebuilt. It had **two** stages — pick a zone, rest an order at
it — and with nothing to wait for it committed immediately and let the market
come to it. On a level a fortnight away that is not a trade, and that is what
put the order the owner reported days of travel from price.

It now has three, and **only the third may be placed**:

| Stage | Meaning | Placeable |
|---|---|---|
| `armed` | The zone is chosen, price has not reached it | No |
| `waiting` | Price is at the zone, the 30m has not reacted | No |
| `triggered` | Price is at the zone AND the 30m has shifted or engulfed | Yes |

**Both gates are required.** Arrival alone is an order resting in front of a
market that has not got there; a reaction anywhere on the chart is a trade with
no level behind it. `trigger.has_arrived` is the first, `trigger.evaluate` the
second.

The first two stages are still **returned and shown**, with their levels, so
the plan is visible before it is live. What makes them unplaceable is
`setup.invalidations` — the same mechanism that refuses a thin ratio, so
Execute is disabled with a reason rather than mysteriously. The card renders
those two in amber as "not ready" rather than in red as a rule breach, because
waiting is the method working: most of the time there is no trade.

**A triggered setup enters at the market.** The guide says "enter immediately
after a signal candle closes", and the whole point of waiting for the 30m is
that price is already at the level when it fires — there is nothing left to
rest an order for.

### Shift of structure

`structure.shift_of_structure` is the heavier of the two triggers (10% on the
community checklist, against 10% for the engulfing — but a shift is a statement
about structure and an engulfing is one bar, so a shift outranks it here).

Two properties are load-bearing:

- **The close, never the wick.** A wick through a level tests it; only a close
  says it changed hands. Without this every spike into a level is an entry, and
  spikes into levels are exactly what a good level produces.
- **The bar that CROSSED, not every bar after it.** Price sitting above a level
  it broke twenty bars ago is not a fresh trigger. Without the cross check the
  recency window means nothing, because the most recent bar keeps satisfying
  "close is above the level" for as long as the move lasts.

### What is still not built from the 30m group

`trigger.round_level` implements the checklist's Round Psychological Level
(5%) and is **not yet scored by the confluence checklist** — it is available
and unused. Wiring it in changes `MAX_SCORE`, which changes every grade on the
page, so it is a separate change.

## A resting order has to be reachable (2026-09-21)

The owner placed a resting order and found from the chart that price might take
days to reach it. `propose` was taking the nearest qualifying zone with **no
ceiling on the distance at all** — and across months of daily and weekly levels
the nearest one can be hundreds of dollars off. A perfectly real level, and not
one this week's order belongs at.

`MAX_ENTRY_DAILY_ATR = 3.0` is the ceiling, measured in **daily** ATR rather
than points, because "how far away" only means anything as "how long would it
take": 300 points is a fortnight on a quiet gold and two sessions on a violent
one. Three days rather than five because price meanders, so a level three
average days off is roughly a week of real travel — and a week is the horizon
the method plans over ("2-3 hours weekly" is its whole pitch).

The candidate now carries `distance` and `distance_days`, and the card and the
confirmation dialog both state the wait. That is half the fix: a resting order
that fills tomorrow and one that fills in a fortnight rendered identically —
same card, same numbers, same green Execute button — so the wait has to be
said, not left to be judged off the chart.

`distance_days` is `None`, never `0`, when the daily range cannot be read. An
unknown wait rendered as "0 days" reads as "fills immediately", which is the
most encouraging possible wrong answer about a trade.

## OPEN: the resting order expires after four hours

`manual_limit_order._DEFAULT_EXPIRE_MINUTES` is **240**, the EA honours it
(`ForexTraderBridge.mq5` reads `expire_minutes`, default 240.0), and
`api/routers/orders.py` offers no way to override it. So a Set & Forget order
resting at a zone a day away is withdrawn by the broker about four hours after
it is placed, having never had a chance to fill.

That constant is shared with the manual Limit Order dialog and the Telegram
`[LIMITS]` path, both of which have always had it, so changing it in place
would change how those behave too — golden rule 3. The shape that is safe is an
optional `expire_minutes` on `open_manual_limit_order` keeping 240 as the
default, so every existing caller is byte-identical and Set & Forget passes its
own.

**Not done.** It is `services/trading/`, it is the order path, and it needs
owner sign-off and a demo session per `docs/system/rules/20-trading-safety.md`.

## The money boundary

**No endpoint under `/api/trading/setforget` places, closes or modifies
anything.** The Execute button posts to `api/routers/orders.py` —
`/orders/market` and `/orders/limit`, the same two the manual dialogs use — so
this app still has exactly one order path.

This is asserted rather than assumed:
`tests/api/routers/test_setforget.py::TestItCannotPlaceAnything` checks that
the sentinel engine's money methods are never reached AND that no route in the
module is named for execution. The second is the negative control: a future
`/setforget/execute` would pass the first test and fail the second.

`setup.money_at_risk`, `money_at_target` and `lot_from_risk` all forward to
`trading/fees_sizing`. The browser receives a **per-lot** cash figure and
multiplies it, so the position box can re-price as the lot selector moves
without a second P&L implementation in TypeScript.

## What the AI is and is not allowed to do

The deterministic candidate is built before the model is asked anything. The
model reviews it; it does not find it. Everything it returns about entries,
stops and targets goes back through `setup.build` and `setup.invalidations`,
and what fails is discarded with the rules' own levels left standing and
`ai.levels_rejected` shown on screen.

This matters because a model asked for a stop and a target produces a stop and
a target every single time, for every chart, including ones with nothing on
them. Pinned by
`test_analysis.py::TestEvaluate::test_a_model_answer_that_breaks_the_rules_is_discarded`.

The model is not called at all when there is no candidate — paying to be told
what the rules already said, when the reason is already on screen.

**The model named on screen comes from `provider.active_model(cfg)`, never
from `claude_model` directly.** `claude_model` carries a config default
(`config/__init__.py`) that is present whether or not Claude is the selected
provider, so the obvious-looking `cfg.get("claude_model") or
cfg.get("deepseek_model")` always resolved to Claude. Between 2026-09-18 and
2026-09-21 every DeepSeek review was captioned `claude-sonnet-4-6`, and the
Execute button's tooltip said it would bill a model it never called. Routing
was never affected — `complete()` always branched on `ai_provider` correctly —
so this was a lie in the label only, which is exactly the kind that survives a
green suite. `active_model` branches identically to `complete()`, including
the fall-through to Claude when `ai_provider` is unset, so the caption cannot
describe a different call than the one that was made.

## Three things the tests did not find on their own

Worth recording because both were invisible in a green suite, and both were
found in the first ten minutes of looking at the page with synthetic candles
behind it (`tools/` has no harness for this; a scratch FastAPI app with a fake
engine did it).

1. The position box was laid out perfectly and painted **underneath** the
   candles — lightweight-charts stacks its own panes, and an HTML overlay with
   no z-index of its own loses. The overlay is `z-20`, the AOI bands `z-10`.
   jsdom has no layout, so `SetupChart.test.tsx` asserts the classes and says
   in a comment why that weak assertion is still worth having.
2. The price scale fitted the **candles**, so a resting entry a few hundred
   points below them fell off the bottom, `priceToCoordinate` returned null for
   every level, and the box silently did not draw. Fixed with an
   `autoscaleInfoProvider` that widens the range to cover the setup's own
   levels; the provider is called directly in the test rather than trusted.

3. The chart named its own hex colours — `#030712` on a background, `#00cc88`
   on a candle — so it was a black rectangle inside a white panel in light
   mode, three clicks from a Chart tab that themed correctly. A canvas cannot
   read a CSS variable, so every chart in this app has to be TOLD its colours
   and re-told them when the theme changes; `chartTheme.ts` is the one place
   that answers. It moved from `chart/internal/` to `shared/` when this became
   its third caller, because a domain's `internal/` is not for other domains
   to import.

4. `new ResizeObserver` was unguarded in `useChartGeometry`. Where there is
   none — jsdom, an older Safari, a server render — the constructor throws
   inside a passive effect and React unmounts the tree: the chart does not
   degrade, the whole tab goes blank. It now falls back to a `window.resize`
   listener, which misses a container that resizes without the window and
   costs a stale overlay until the next pan. That is the cheaper failure.

   This one surfaced as a FLAKE, not as a bug report. `SetForgetPanel.test.tsx`
   stubbed the global in `beforeEach` and cleared it in `afterEach`, and a
   passive effect landing after the clear failed about one full-suite run in
   three — pointing at whichever test was last rather than at the missing
   guard. The stub now lives in `frontend/src/test/setup.ts` for the whole
   suite, because jsdom genuinely has no ResizeObserver and every chart test
   needs one.

The first two are the same shape of bug: the code was right, the picture was
wrong, and nothing in a unit test looks at a picture. The third is a different
and worse shape — an intermittent red that is easier to re-run than to read.

## Open questions

- **Nobody has backtested this.** The 60-65% win rate quoted around the method
  is third-party marketing, not a measurement of this implementation. The
  zone-drawing rules in `aoi.py` are a defensible reading of the method, not
  the only one, and a different reading would produce different trades.
  Measuring it needs a study like `reversal_engine/research_lab.py`.
- The stop buffer is `0.25 × ATR` (`analysis.STOP_BUFFER_ATR`) — a reasoned
  choice, not a measured one. The zone merge gap is **settled**: there is no
  ATR in the marking at all, see above.
- **A timeframe that cannot complete a pair still contributes its nearest
  validated band, and that band can be old.** Live on 2026-09-21 the weekly had
  no validated supply above price, so it contributed a demand last touched in
  August 2025, 1,000 points below price. It does not reach the candidate today
  because the daily's 4282 demand is nearer and `next_opposing` takes the
  nearest — but if price broke that daily level the 2025 band becomes the
  target, and the thousand-point-target pathology is back. The alternative is
  for a timeframe that cannot produce a pair to contribute nothing. **Owner's
  call; not taken.**
- **The daily zone window is far too long, and it is degrading live output.**
  `DAILY_COUNT = 400` exists so `resample.to_weekly` has ~2 years to read a
  weekly bias from. But the same 400-bar series is also handed to
  `aoi.zones(daily, ...)`, which does not need that depth and is ruined by it.
  On 2026-09-21 the window spanned 565 calendar days back to 2025-03-05, over
  which gold trended 3600 → 5250. Raw daily swing bands are 129–276 points
  wide each; across that range they tile it and overlap, and `merge` folds
  same-kind overlapping bands transitively into one. Measured that day:

  | daily bars | window starts | widest band |
  |---|---|---|
  | 400 (current) | 2025-03-05 | 1035 pts |
  | 180 | 2026-01-12 | 979 pts |
  | 120 | 2026-04-07 | 441 pts |
  | 60 | 2026-06-30 | 118 pts |

  This happens at `gap=0` too, so it is the lookback and not `ZONE_MERGE_ATR`.
  The live candidate that day was a market SELL at 4350.82 with a 4897.73 stop
  — 547 points of risk — entered at an "897-point supply zone" (3991.92 –
  4889.38, 80 touches) that simply contained the current price, targeting a
  2025-09-17 demand zone. That is the exact failure `aoi.py`'s own header
  warns about: a zone wide enough to swallow price makes every candidate read
  as at-a-zone, and the method becomes "trade whenever".

  The likely fix is to keep 400 bars for the weekly bias and pass a shorter
  slice to the zone detector. **How short is a trading-policy call and needs
  the owner plus a demo session** — it moves entry, stop and target on a
  button that places orders. Not changed.
- The section reads XAUUSD only, per the owner's instruction 2026-09-19. The
  services take a candle series and know nothing about the symbol, so widening
  it is a bridge question, not a maths one.
