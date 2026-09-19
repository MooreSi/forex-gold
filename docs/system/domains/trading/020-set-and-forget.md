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
| The section | `frontend/src/components/setforget/` |

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

**Zones merge by proximity, not just by overlap** (`ZONE_MERGE_ATR = 1.5`).
Found by looking at the running page, not by a test: over a 400-bar window the
detector produces a dozen bands stacked within a few points of each other, so
`next_opposing` always returns something a point or two from the entry, every
candidate comes out under 1:2, and the section refuses everything it finds —
for a reason about the detector rather than about the chart. A trader drawing
that same chart draws four or five chunky levels. `analysis.gather` derives the
gap from ATR and hands it to both `aoi.zones` and `aoi.merge`; the wiring is
pinned by `test_analysis.py::TestTheZoneMergeGapIsWired`, because the merge
could be perfect and the orchestrator could still call it with the default of
zero.

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

3. `new ResizeObserver` was unguarded in `useChartGeometry`. Where there is
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
- The stop buffer is `0.25 × ATR` (`analysis.STOP_BUFFER_ATR`) and the zone
  merge gap is `1.5 × ATR` (`analysis.ZONE_MERGE_ATR`). Both are reasoned
  choices, not measured ones. The merge gap in particular decides how many
  levels the chart carries, and therefore how often a setup exists at all.
- The section reads XAUUSD only, per the owner's instruction 2026-09-19. The
  services take a candle series and know nothing about the symbol, so widening
  it is a bridge question, not a maths one.
