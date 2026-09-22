# XAUUSD session behaviour — what the price history actually says

**Measured 2026-09-22** against the live Vantage MT5 bridge. Everything below is
reproducible from `/candles_range`; nothing here is received wisdom about what
"the London session does".

## The data

| Set | Bars | Span | Notes |
|---|---|---|---|
| H1 | 50,956 | 2018-03-15 → 2026-09-22 | continuous; before 2018-03 MT5 holds only sparse weekly bars |
| M15 | 67,065 | 2023-11-20 → 2026-09-22 | MT5 keeps no M15 further back than this |

Pulled by paging `candles_range` in 90-day (H1) and 30-day (M15) chunks.

**Do not ask the bridge for a huge count.** `_get_candles_range` sizes its
`copy_rates_from_pos` call from `now - from_ts`, not from the chunk width, so an
old chunk is a big fetch however narrow the window. A single
`candles_symbol?count=200000` killed the bridge process outright on 2026-09-22;
the SelfHealer restarted it and the app recovered in ~90 seconds, but it was a
real outage of the broker connection. Chunks of a few thousand bars are fine.

## Gotcha: the one-hour DST smear in historical candles

`_get_candles_range` converts MT5's server-time bars to true UTC using an offset
it measures **on the call** (`tick.time - time.time()`). That offset is correct
for now and applied unchanged to every bar returned, including bars from the
other half of the DST year — when the broker's own clock was one hour different.

The tell: the daily maintenance break sits at exactly 21:00 in **every** bar of
the 8.5-year sample. In true UTC it is 21:00–22:00 from April to October and
22:00–23:00 from November to March, so a genuinely-UTC sample would split the
gap across two hours. It does not.

Net effect: a long historical pull is in a fixed frame equal to **summer UTC**.
For session work that is arguably the better frame — it holds sessions at
constant local business hours year-round — but it is not UTC, and anything
joining these bars to a stored `ts` from the app will be an hour out for half the
year. Recent bars (same DST half as the call) are correct.

## Related gotcha: the app's own session boundaries are fixed UTC

`market/sessions.get_session` reads `datetime.now(timezone.utc).hour` against
fixed boundaries. Those boundaries are right in the northern winter and **one
hour late from April to October**: the measured London volatility expansion
begins at 07:00 UTC in summer, and `london` does not start until 08:00.

The measured hourly profile puts the expansion at local 08:00 in both DST
halves, as it should. It is the fixed-UTC window that moves relative to it, not
the market.

## Hourly volatility profile (H1 range, basis points of price, 2018-2026)

```
UTC   00   01   02   03   04   05   06   07   08   09   10   11
      27   30   22   18   18   24   28   30   27   24   24   27
UTC   12   13   14   15   16   17   18   19   20   21   22   23
      47   49   48   36   27   26   25   23   16    -   23   19
```

- **12:00–15:00 carries roughly twice the volatility of any other hour.** This is
  the single largest and most stable feature in the whole dataset.
- 02:00–04:00 is the true dead zone (18 bp), not "the Asian session" as a whole:
  22:00–01:00 is materially livelier (19–30 bp).
- 07:00 is a local maximum and 09:00–10:00 is a lull. The London open burst is
  real, and it is over within about two hours.
- 20:00 (16 bp) is the quietest hour of the entire trading day.
- Hour 21 is the daily break — 3 bars in 8.5 years, all rollover artefacts.
  Exclude it from any aggregate or it poisons the mean.

## Session ranges

Sessions as `market/sessions.py` defines them: asian 23–08, london 08–12,
overlap 12–17, ny 17–21.

| Session | hours | median range, full M15 sample | median range, last 250 days | per hour (recent) |
|---|---|---|---|---|
| asian | 9 | 261 pips | 546 pips | 61 |
| london | 4 | 157 pips | 295 pips | 74 |
| overlap | 5 | 307 pips | 571 pips | 114 |
| ny | 4 | 131 pips | 286 pips | 72 |

**Ranges have roughly doubled in the current regime.** Any pip constant written
before 2025 is now calibrated to a market that no longer exists.

**The overlap posts the day's largest per-hour range on 61–78% of days, in every
one of the nine years measured.** London 15–25%, NY 4–15%, Asian 0–7%. The Asian
share has risen from ~1% (2018–2024) to ~7% (2025–2026) — a real regime shift,
worth re-checking rather than assuming it continues.

## Three strong negative results

These kill more strategy ideas than the positive findings enable, so they are
worth stating plainly.

1. **Direction does not carry between sessions.** Same-sign move from one session
   to the next: asian→london 50.3%, london→overlap 49.2%, overlap→ny 49.8%
   (n≈2,175 days each). A coin flip, to within a rounding error. "Trade London in
   the Asian direction" has no support whatsoever.

2. **No session systematically closes at its extreme.** Mean close position
   within the session's own range is 0.507–0.528, and mean distance from the
   midpoint is 0.24–0.26 — which is what a uniform distribution gives (0.25).
   There is no "hold it to the session close" edge in any session.

3. **Trade geometry on a random entry is a wash.** A grid over stop ∈ {1.0…2.5}×ATR
   and TP1 ∈ {0.75…2.0}×stop, flat-TP against partial-plus-runner, returned
   expectancies inside ±0.03R for every session and every combination. That is
   noise, and it was measured on ~2,800–26,000 entries per session, so it is not
   a sample-size problem. **Geometry does not create edge.** It only changes how
   an edge that already exists is harvested. Do not let a grid search like this
   argue for a template; it cannot.

## What does differ between sessions

The sessions differ in **how far price travels relative to the volatility that
was already visible when the trade was entered**. That is the property a template
can be built around.

Survivor MFE at a 1×ATR stop, divided by that same ATR (full M15 sample):

| Session | stop hit % | MFE p50 / ATR |
|---|---|---|
| london | 41.3% | **1.16** |
| overlap | 24.1% | 0.64 |
| asian | 20.8% | 0.61 |
| ny | 12.5% | **0.42** |

- **London expands.** The ATR measured at London's open reflects the quiet hours
  before it, so it systematically understates what follows. London both stops out
  most often at a given multiple and travels furthest. It is the only session
  whose optimum sits at a wider target: over 2018-2026 it reached +3R on 20.4% of
  entries against 5.6–12.6% for every other session.
- **The overlap is large but contained.** Highest absolute volatility, lowest
  extension relative to it. Big moves, but they do not exceed what the ATR
  already implied. Take profit into them rather than running them.
- **NY (17:00–21:00) is the weakest window in the day.** Lowest extension, lowest
  volatility, decaying into a 16 bp final hour and then the break. Nothing in
  9 years of data recommends it.

## The Asian-range break (n=2,176 days, 2018-2026)

Measured as: mark the Asian session's high and low, then watch London.

- London breaks that range on **70.5%** of days.
- It breaks **both** sides — a genuine whipsaw — on only **2.7%**.
- Of the clean one-way breaks, **52.6%** are still beyond the broken level when
  the overlap ends.
- From the break level to the end of the overlap: MFE median 0.71× the Asian
  range, MAE median 0.61×. Close to symmetric at the median.
- The tails are not symmetric: P(MFE ≥ 1.5× Asian range) = **19.5%** against
  P(MAE ≥ 1.5×) = **12.9%**.
- The break's extension **roughly doubles after London ends** — median 0.27× the
  Asian range at London's close, 0.67× by the overlap's close.

Read together: a modest win rate with a fat right tail, and most of the payoff
arriving after the session that produced the signal. That argues for partials
plus a surviving runner, and against flattening at London's close.

## Stop calibration, current regime

Random entry, both directions, 6h horizon, adverse extreme assumed first within
a bar (the conservative assumption `backtest/template_simulator` also makes).

| Stop | asian hit% | london hit% | overlap hit% | ny hit% |
|---|---|---|---|---|
| 80 | 76.4 | 83.8 | 77.3 | 77.6 |
| 120 | 67.1 | 75.6 | 69.6 | 67.4 |
| 200 | 49.3 | 61.5 | 55.1 | 49.6 |
| 250 | 40.2 | 54.0 | 45.5 | 41.9 |
| 400 | 21.4 | 31.6 | 26.6 | 24.6 |

**A sub-200-pip stop on XAUUSD is inside the noise in this regime.** The 50-pip
stop carried by `30 TP1 SL50 and Trail` — in live use on three channels — is hit
on the overwhelming majority of random entries within six hours. It is not a stop
so much as a toll.

Survivor favourable excursion, at the stop where P(reach 1R) peaks for each
session:

| Session | stop | p25 | p50 | p75 | p90 |
|---|---|---|---|---|---|
| asian | 200 | 193 | 306 | 480 | 689 |
| london | 250 | 301 | 427 | 654 | 943 |
| overlap | 200 | 217 | 348 | 543 | 853 |
| ny | 200 | 175 | 296 | 465 | 723 |

## Spread

476 real fills on the demo account (2026-09-03 → 2026-09-21), by session:
median 2 pips, p90 2–3 pips, p99 3 pips — **flat across all four sessions**.

Caveats that matter: three weeks, demo, and the sample contains no major news
spike and no 22:00 rollover fill. It is evidence that the default
`max_spread_pips: 6.0` is loose in normal conditions, not evidence that a tight
gate is safe at rollover.

## Gotcha: what `use_dynamic_atr` actually measures

The ATR behind `atr_sl_mult` / `atr_tp1_mult` comes from `dpm_candles`, which
`positions/monitor_cycle.py` fills with `get_candles("M5", 30)`. Wilder ATR(14)
over 30 M5 bars is a **~70-minute** volatility window.

At a session boundary — exactly when a session-scoped template engages — that
window sits inside the *previous* session. For a London template that is the
quiet pre-open hour, which is the precise case where the measurement above says
ATR most understates the move to come. `use_dynamic_atr` is therefore the wrong
tool for a session template, and the four session templates leave it off and use
fixed pips.

To make it the right tool, the ATR would need to come from a timeframe long
enough to span the session it is sizing — an H1 or H4 series, not 30 M5 bars.
That is a change to `monitor_cycle`, not to a template.

## Open questions

- The Asian session's share of the day's largest range went from ~1% to ~7%
  between 2024 and 2026. Regime shift or artefact of the 2025-26 gold rally?
  Unmeasured.
- The extension asymmetry favouring longs (gold's 2018-2026 bull trend) is
  present in every session and strongest in NY. It is a drift, not a structure,
  and nothing should be built on it without a bear-market sample to test against.

---

# Backtest: does any of this beat the template already in use?

**Measured 2026-09-22.** Short answer: **no.** Recorded against
`30 TP1 SL50 and Trail`, which is live on three channels, the session templates
lose in every session on the large sample. This section exists so that is on the
record next to the research that produced them.

## Gotcha that nearly produced the opposite answer

`re_signals.stop_loss` is the **current** stop, not the entry stop. Every winner
whose stop trailed past entry stores a stop on the "wrong" side of the entry
zone, and the standard `BUY -> sl < entry_low` sanity check therefore rejects it.

Applying that check to `re_signals` drops **3,572 of 6,442 rows, of which 3,549
(99.4%) are wins.** The surviving sample is 53% losses against a true population
that is ~61% wins, and every template walked over it looks catastrophic.

Reconstruct the entry stop as `entry_mid ± sl_dist` instead. That reproduces the
stored `stop_loss` exactly on all 2,870 rows whose stop never moved, and recovers
the full 6,441-signal universe. `vantage_signals.stop_loss` does **not** have this
problem — its 58 wrong-side rows are degenerate stops sitting on the zone edge,
which are genuinely unusable.

## What was walked

The app's own `template_simulator.simulate` and `engine._simulate` fill rule,
over 18,000 M5 bars (2026-06-22 → 2026-09-22), spread 0.4, commission $7/lot,
every result normalised to a 0.01 lot so templates with different anchor lots
compare. Two signal sets:

- `vantage_signals` — 566 signals, all channels, 2026-09-03 → 2026-09-22; 350
  usable. This is what the Backtest tab walks.
- `re_signals` — 6,441 reversal-engine signals, 2026-07-23 → 2026-09-22; 6,441
  usable once the stop is reconstructed. The Backtest tab **cannot** reach these:
  `signals_from_db` reads `vantage_signals` only, and just 864 re_signals were
  ever mirrored there.

## Result, reversal-engine signals (per 0.01 lot)

| Session | n | first cut | session-scaled | `30 TP1 SL50` (live) |
|---|---|---|---|---|
| asian | 2,392 | +1,345 (PF 1.06) | +3,822 (PF 2.41) | **+4,113 (PF 2.32)** |
| london | 1,011 | -1,122 (PF 0.92) | +1,710 (PF 1.95) | **+1,738 (PF 2.34)** |
| overlap | 1,795 | +2,030 (PF 1.12) | +2,099 (PF 1.36) | **+2,431 (PF 1.86)** |
| ny | 892 | +660 (PF 1.08) | **+1,250 (PF 2.00)** | +1,221 (PF 1.93) |
| **all** | 6,090 | +2,913 (PF 1.05) | +8,881 (PF 1.77) | **+9,503 (PF 2.11)** |

Holds out of sample: splitting the triggered signals at 2026-08-25 gives the live
template the win in both halves (3,870 vs 3,592 and 3,612 vs 3,306). Excluding
the 972 signals that expired without ever trading — the walk's most profitable
cohort at PF 3.27, which is itself a warning about how optimistic it is — the
ranking is unchanged (7,481 vs 6,898 over 5,240 triggered signals).

## Why the first cut failed

Stops of 200-250 pips and first targets of 175-300 pips, calibrated in the study
above on **random entries**. A random entry has no edge and needs a wide stop to
survive session noise. A signal entry is not a random entry: it is a short-horizon
call with its own invalidation, and the reversal engine's mean stop is 5.75 points
= 57.5 pips. Widening the stop to 200 pips does not buy survival, it just makes
each loss four times larger, while a target at 175-300 pips sits beyond where
these signals travel at all. Win rate fell to 43-56% against the live template's
69-75%.

Rescaling the same session *shape* down to a ~50-pip anchor recovered most of the
gap (+2,913 → +8,881) and is what the four templates now hold. It still does not
beat the incumbent.

## The finding worth keeping

**Session character does not transfer to signal trade-management.** The session
differences measured above are real in gold's price paths and are stable over
8.5 years, but the binding constraint on a signal trade is the **signal's own
horizon**, not the session's. Geometry has to be sized to the distance the signal
resolves over. Anything derived from unconditional session statistics is sized to
the wrong thing.

Where session structure could still pay is in **admission** — whether to take a
signal at all in a given window, and at what size — which is a different mechanism
from a template and is not tested here.

## Caveat that outranks all of the above

The walk says the live template earns **+1.56 per 0.01 lot per trade**. The demo
account's actual executions over 2026-09-03 → 2026-09-22 lost money in every
session (-$4.23 to -$9.71 per trade, 1,795 trades). The backtest is materially
more optimistic than reality — it fills at the zone midpoint on any bar that
touches the zone, models no slippage beyond a fixed spread, no swap, and no
missed or late fill. **Treat every number in this section as a ranking, not a
forecast.** Reconciling that gap is unfinished work.

---

# The backtest disagrees with the ledger on 1 trade in 5

**Measured 2026-09-22.** The section above ended by saying the walk is optimistic.
It is worse than that, and this is the number to quote.

666 reversal-engine signals reached the broker and have a recorded P&L. Walking
those **same signals** under the template that actually managed them:

| | win rate | result |
|---|---|---|
| what really happened | 57.4% | **-$208.46**, -$0.31/trade |
| the walk, same signals, same template | 75.0% | +$1,133 per 0.01 lot |

Outcome-by-outcome against the engine's own labels:

| real | walk | n |
|---|---|---|
| loss | win | **135** |
| win | loss | 18 |
| loss | loss | 148 |
| win | win | 361 |

**The walk turns 135 of 283 real losses into wins, and only 18 real wins into
losses — a 7.5:1 asymmetry.** It does not merely overstate the size of the
result; it gets the sign wrong on 20% of trades, nearly always in its own favour.

The likely mechanism is the fill: `engine._simulate` fills at the zone MIDPOINT
on the first bar whose range overlaps the zone. A bar that sweeps through a
3-point reversal zone in one move gets a midpoint fill it would never have got
live, and that better entry then rescues a stop that really was hit. Nothing
models slippage beyond a fixed spread, a late fill, or a missed fill.

## What this means for using it

- **Do not read absolute numbers from the Backtest tab at all.** A template
  showing PF 4.03 and an 87% win rate on this walk is showing an artefact.
- **Treat even the ranking as weak.** The bias is not neutral between templates:
  an optimistic entry rescues a tight stop more than a wide one, so the walk
  systematically favours whichever template has the tightest stop for a given
  target.
- The one conclusion strong enough to survive it is a **negative** one: a
  template that ranks last across every cut, with an understood failure mechanism,
  is genuinely bad. That is the basis on which the four session templates were
  rejected, not the size of the gap.

Fixing the fill model — pessimistic fill at the far edge of the zone, or a tick
walk over the fill bar — is the prerequisite for the Backtest tab being usable
for a money decision. Until then it ranks, badly, and it forecasts nothing.
