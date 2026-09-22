# What actually manages a trade well

**Measured 2026-09-22.** Supersedes the conclusions of
[020](020-xauusd-session-behaviour.md) about templates. The session research in
that file is sound as market description and wrong as a basis for trade
management; this file says why, and what replaced it.

## First: the backtest was broken, and is now fixed

`engine._simulate` filled every zone signal at the **midpoint** of its entry
zone. The live path fills when price ENTERS the zone, which is its near edge --
and for a signal placed against the move that is the worst price in the zone.

Measured against 664 reversal-engine signals with a recorded `trigger_price`:

| where the real fill landed in the zone | |
|---|---|
| p25 | 0.84 |
| median | **0.95** |
| in the worse half | **98.1%** |

| modelled fill vs real fill | |
|---|---|
| midpoint | **$1.34 better than reality** = 21% of the stop distance |
| adverse edge | $0.16 worse than reality |

The consequence was not a small optimism. On 666 real executions the old walk
scored **135 of 283 real losses as wins**, against 18 the other way: the wrong
sign on one trade in five, almost always in its own favour.

Fixed in `engine.entry_fill_price`, pinned by
`tests/backtest/test_fill_price_is_zone_edge.py`. Calibration after the fix,
same 666 executions, same template that really managed them:

| | win rate | avg per trade |
|---|---|---|
| the real ledger | 57.4% | **-$0.33** |
| the walk | 53.3% | **-$0.17** |

Sign agreement went from 135-vs-18 optimistic to 49-vs-74 -- now marginally
**pessimistic**, which is the correct direction to err. A residual ~$0.16 per
trade of optimism remains; subtract it before believing any number below.

A second fix: `template_support.summarise` reported every `use_dynamic_atr`
template as un-backtestable, although `_simulate_template` supplies `_atr14`
and walks them fine. The picker therefore hid the only class of template that
adapts to anything. Pinned by
`tests/backtest/test_dynamic_atr_is_offered_to_the_picker.py`.

## The method that settles arguments

For signals that actually executed, the engine records `trigger_price` and
`trigger_time`. Starting each simulated trade from the **real fill price at the
real fill time** removes the fill model from the comparison entirely, leaving
only the trade management being compared. Every number below is measured that
way, on 666 real executions, and normalised to a 0.01 lot.

Use this in preference to the Backtest tab whenever the question is "which
management is better", because it cannot be wrong about entries.

## Result

| template | win% | PF | avg $/0.01 lot |
|---|---|---|---|
| **Adaptive Volatility v1** (stop = 1.6 x M5-ATR) | 77.3% | **1.61** | **+1.05** |
| Reversal Measured v1 (fixed 70p / 150p) | 74.3% | 1.44 | +0.80 |
| GD VIP - Single | 71.0% | 1.34 | +0.59 |
| ... | | | |
| `30 TP1 SL50 and Trail` (in live use) | 53.3% | 0.93 | **-0.17** |
| Asian Range Fade / Overlap / NY / London (session set) | 45-51% | 0.90-1.04 | -0.23 to +0.10 |

Fitting the multiplier on the first half of the trades and reporting the
second half untouched:

| holdout (n=333, never used to choose anything) | win% | PF | avg |
|---|---|---|---|
| Adaptive, stop = 1.6 x ATR | 78.4% | 1.67 | +1.16 |
| Reversal Measured v1, fixed | 73.9% | 1.48 | +0.89 |
| `30 TP1 SL50 and Trail` | 53.5% | 0.93 | -0.16 |

The fit surface is flat: every `tp_ratio` from 1.5 to 3.0 scores within 0.03,
and `sl_mult` 1.6-2.0 is a broad plateau. This is not a knife-edge optimum.

## Why the session templates failed

They were sized from **unconditional session statistics** -- how far gold
travels in a session from a random entry. A random entry has no edge and needs
a wide stop to survive noise. A signal entry is a short-horizon call with its
own invalidation, and the reversal engine's stops average 57.5 pips. A 200-pip
stop does not buy survival; it makes each loss four times larger while the
target sits beyond where the signal ever travels.

**Session character does not transfer to trade management.** The session
differences in [020](020-xauusd-session-behaviour.md) are real and stable over
8.5 years. They describe the market, not the signal.

## Volatility does transfer -- but only to the stop

This is the finding worth keeping.

Sizing the stop and the whole ladder from the live ATR beats a fixed distance
**at the same median stop size** ($7.2 adaptive vs $7.0 fixed): PF 1.61 against
1.44. The gain is not from being wider, it is from being wider when volatile
and tighter when calm.

Scaling the **trail** the same way makes it worse, and worst exactly where the
intuition says it should help:

| M5-ATR tercile | fixed trail | trail scaled with ATR |
|---|---|---|
| low | +0.89 | +0.86 |
| mid | +0.78 | +0.80 |
| **high** | **+1.47** | **+0.56** |

In a volatile market the move is larger, so a tight trail banks more of it. A
trail widened "to give it room" hands the extra move back. **Volatility widens
the stop; it must not widen the trail.**

Note also that the 70-minute M5 ATR the live path feeds a template (median
$4.50) and a 14-hour ATR at the same moments (median $4.63) produce nearly the
same result. [020] speculated the short window was the wrong tool; on this
evidence it is not, and that speculation is withdrawn.

## Per channel, on real fills

| channel | n | actual ledger | best simulated management |
|---|---|---|---|
| Reversal Engine | 230 | -0.93 | **Adaptive +0.92** (PF 1.51) |
| Gold Diggers VIP | 133 | -0.87 | **Adaptive +0.92** (PF 1.47) |
| GOLD DIGGERS INSTITUTIONAL | 123 | -1.46 | best is -0.22; **nothing is positive** |

**INSTITUTIONAL cannot be fixed by trade management.** Its actual win rate is
38.2%, and the best of thirteen templates still loses. That is an entry-quality
problem, and retuning the exit is the wrong lever.

## Open questions

- `Reversal Measured v1`, the base this was derived from, was created
  2026-09-22 06:19 and is in no doc -- almost certainly fitted on this same
  data. The adaptive variant was re-fitted with a genuine time holdout, which
  is the best available control, but no truly independent sample exists yet.
- All of it is two months of one strongly trending gold market.
- There is no ATR multiplier for the trail fields, so "tight trail, wide stop"
  currently has to be written as fixed pips and does not travel across
  regimes the way the stop does. Whether it needs to is unmeasured -- the table
  above says a fixed trail is right, so this may need nothing at all.

---

# Why Adaptive Volatility v1 failed live (2026-09-22)

It was bound to the reversal engine and Gold Diggers VIP and took six trades:
**net -$202 on 0.1 lots.** Three "winners" of +$6.40, +$5.00 and +$10.20;
three losers of -$70.10, -$83.10 and -$70.40. Average win $7.20, average loss
$74.53 — a payoff needing a **91% win rate** to break even.

It is worth being precise about how the recommendation in the section above
came to be wrong, because three separate defects compounded and each one is
still there for the next template.

## Defect 1 — the trail was narrower than any bar the walk could use

`simulate` tests the stop using the level it held at the END of the previous
bar, then advances the trail from THIS bar's high. Inside one bar the trail
can move and never be tested. The live EA trails and tests on every tick.

That error is invisible while the trail is wide. Once the trail is narrower
than the bars, the walk simply cannot see the exit happening. The signature is
an edge that **grows the coarser the bars get** — 261 real fills, one
template, identical entries, only the bar size changing:

| bars | win% | avg win | avg loss | PF | avg $/0.01 lot |
|---|---|---|---|---|---|
| M1 | 65.9% | 2.20 | -3.49 | 1.22 | +0.26 |
| M5 | 79.8% | 3.64 | -8.27 | 1.74 | +1.24 |
| M15 | 82.9% | 5.70 | -14.46 | 1.92 | +2.27 |
| H1 | 83.1% | 12.14 | -31.23 | 1.91 | +4.80 |

A real edge does not depend on the resolution it is measured at.

Median bar ranges in this regime are **$1.73 (M1), $4.27 (M5), $7.23 (M15),
$16.68 (H1)** against a **$1.50** trail. It was unsimulatable on every series
available, including the finest — which is why even the M1 figure (+0.26) is
still optimistic against the live result.

Note the direction of the bias is not the same for every template.
`30 TP1 SL50 and Trail`, with a $3.00 trail and a $5.00 stop, gets *worse* on
coarser bars (M1 -0.15 → H1 -1.47) because its stop is wider than the bars and
only its TP suffers. So comparing the two on M5 systematically favoured the
one with the tighter trail. **The M5 comparison that produced the
recommendation was structurally rigged toward the wrong answer.**

Now refused: `template_simulator.trail_inside_bars_reason`, wired into
`run_backtest`, pinned by `tests/backtest/test_trail_inside_the_bars_is_refused.py`.
On today's candles it refuses Adaptive Volatility v1 and Reversal Measured v1
at every timeframe, and `30 TP1 SL50 and Trail` at M5 and above.

## Defect 2 — `use_dynamic_atr` does nothing at all

Both live call sites are gated on `dpm_candles`:

```
if bool(template.get("use_dynamic_atr")) and dpm_candles:
```

`_dpm_candles` starts `[]` at `runtime.py:224` and is only ever filled at
`monitor_cycle.py:234`, behind `if bool(rs.get("dpm_enabled", 0)) and ...`.
With `dpm_enabled = 0` the list is never populated, so the check is always
false and every ATR-sized template falls back to `sl_pips`.

Confirmed on all six trades: the stop equals the **signal's own** stop to the
cent (7.01/7.01, 7.88/7.88, 8.31/8.31, …). `atr_sl_mult` never applied. The
template was not adaptive in any sense — and the backtest that recommended it
passed `atr=_atr14(...)`, simulating a stop the live path never uses.

## Defect 3 — at 0.01 lots a ladder cannot have partials

`lots = min(round(lot * pct, 2), remaining)`. At `lot_anchor = 0.01`, a 50%
partial rounds to the full 0.01, so the first TP closes everything and every
multi-level ladder collapses to a single exit. Any comparison of ladder shapes
run at 0.01 lots is comparing identical things. (Live runs 0.1, where partials
work — so the backtest and the live path differed here too.)

## The finding underneath all three

None of it matters, because the signals have no edge to harvest.

Barrier race on 261 real fills, M1 bars, stop = the signal's own distance:

| target | P(reach it first) | needed to break even |
|---|---|---|
| 0.25R | 78.2% | 80.0% |
| 0.50R | 66.7% | 66.7% |
| 0.75R | 55.6% | 57.1% |
| 1.00R | 45.6% | 50.0% |
| 1.50R | 33.3% | 40.0% |
| 2.00R | 28.4% | 33.3% |

**Every row is at or below its breakeven probability**, before spread and
commission. The best case is an exact tie at 0.5R. There is no take-profit
distance at which these signals pay, so no exit geometry can rescue them.

And nothing recorded separates the good ones. P(+1R before stop) by bucket,
666 fills: session 44.8-53.6%, direction 47.9-49.6%, level type 46.7-51.5%,
HTF bias 44.7-52.6%, ATR 46.4-50.5%, ADX 45.7-51.8%, ML probability
45.7-51.1%, stop distance 47.4-50.7%. Every spread is ~8 points on samples
where the standard error is 3-5. Nothing clears 50% with any margin.

One result is worth a second look on its own: **`level_score` is inverted.**
The 285 signals the engine scored HIGHEST reach 1R least often (45.6%), below
both the low (50.7%) and mid (51.8%) buckets. The engine's own confidence
ranks its best setups worst.

## What this changes

Stop tuning exits for this engine. The lever is entry selection, and until
P(+1R) clears 50% with margin, every template comparison is rearranging
deckchairs. The three defects above are fixed or documented so the next
measurement is trustworthy; the signal problem is not a measurement artefact
and will not yield to one.

---

# EA changes worth making, and the one that is not

Researched 2026-09-22 against `mql5/ForexTraderBridge.mq5` (4,614 lines).

**None of these makes a losing strategy profitable.** The signal measurement
above says there is no take-profit distance at which these entries pay. An EA
change that fixes the payoff ratio turns a large loss into a smaller one. That
is worth having, and it is not an edge.

## 1. Trail distance as a fraction of the initial risk (the one that matters)

`trail_distance` is absolute pips, applied by `ApplyTemplateStepTrail` with no
reference to the stop it sits inside. The live failure was exactly this: a
$1.50 trail against a $7.00 stop is 21% of risk, so every winner was cut at
+$0.50-$1.02 while every loser paid the full $7-$8.31. Three wins averaging
$7.20 against three losses averaging $74.53 — a payoff needing **91%** to break
even.

The signal's stop is not fixed: it arrives with the signal and ranged $6.13 to
$9.08 across six trades. An absolute pip trail therefore means a different
fraction of risk on every trade, which is not a decision anybody made.

Proposal: `trail_distance_r` on the template, and in `ManagedTrade` a member
holding the stop distance at open. When set, the trail distance becomes
`initial_risk * trail_distance_r` and `trail_distance` is ignored, the same
precedence `use_dynamic_atr` already has over `sl_pips`. A floor of ~0.5R would
have made the live template's trail $3.50 rather than $1.50.

`ManagedTrade` does not currently keep the initial stop — it has `entry_price`
and `trail_dist` but reading `POSITION_SL` later returns a stop that breakeven
and the trail have already moved. So this needs one new struct member set at
open, which is also the prerequisite for anything else expressed in R.

## 2. Refuse a trail that is a small fraction of the stop

Cheaper and blunter than 1, and it can ship on the Python side alone:
`ea_templates` validation rejecting `trail_distance < 0.25 * sl_pips` when
`trail_mode != "off"`. It would have refused Adaptive Volatility v1 (0.21) and
Reversal Measured v1 at the point of saving rather than after six live trades.

It cannot see a signal-supplied stop, only the template's own `sl_pips`, so it
is a guard against the obvious case rather than a fix. Worth having as well as
1, not instead of it.

## 3. What NOT to build: an ATR-scaled trail

The obvious companion to `use_dynamic_atr` is to scale the trail the same way.
It is measurably wrong. 666 real fills, trail scaled proportionally to M5 ATR
against the same trail fixed:

| M5-ATR tercile | fixed trail | scaled trail |
|---|---|---|
| low | +0.89 | +0.86 |
| mid | +0.78 | +0.80 |
| **high** | **+1.47** | **+0.56** |

In a volatile market the move is larger, so a tight trail banks more of it. A
trail widened "to give it room" hands the extra move back, and the damage is
concentrated in exactly the regime the feature is meant to help. Volatility
belongs on the stop; the trail wants to be proportional to **risk**, which is
proposal 1, not to volatility.

## 4. Fix the `dpm_candles` gate, or delete `use_dynamic_atr`

Not an EA change — a Python one, and the honest options are only these two.
Today the flag is on a template, the UI offers it, and it does nothing because
`dpm_enabled = 0` leaves `_dpm_candles` empty. A setting that silently does
nothing is worse than an absent one: it was the entire premise of a template
that then traded real money.

Either populate the candles whenever a template asks for an ATR (a one-line
change to the gate at `monitor_cycle.py:234`, but it changes what stop a live
trade receives), or remove the field so nothing can claim it.

## Deployment reality

Anything in `mql5/` needs a MetaEditor recompile and a demo session before it
governs a trade. None of it is a code change an agent should land unattended —
`ApplyTemplateStepTrail` is on the path that moves a live stop.
