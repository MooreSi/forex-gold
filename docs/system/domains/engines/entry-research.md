# Signal entry correctness and research, 18 October 2026

The owner approved implementing the signal review. These repairs improve the
meaning of entries and measurements. They do not demonstrate positive expectancy.
Existing history, production models and stored trading switches are not rewritten.

## Correctness contract

- Trend PA checks the executable quote, stop risk and existing ATR chase limit,
  scores a separate fill vector and retains the original setup vector. Timed MT5
  quotes older than 60 seconds are refused. The originating M15 close must be
  within one bar when the adapter supplies a timestamp. Legacy adapters without
  timestamps retain unknown timing; their observations must not establish freshness.
- Unicorn management uses `level_type=unicorn`; the former display name is only
  a fallback for legacy rows. Ordinary level ladders retain their own route.
- Reversal virtual barriers and excursions use bid to exit buys and ask to exit
  sells. Executable prices already include spread; only incremental fees are
  deducted. The conservative trail's midpoint anchor remains its original policy.
- Reversal's optional M1 rejection reads completed bars. Its existing exception
  policy still permits entry; an append-only `confirmation` observation distinguishes
  unknown, refused and confirmed states. This is a one-shot gate, not a waiting order.
- A Breakout retest requires an observed cross before the impulsive close. A
  continuation cannot reset the prior-touch count. Creation evidence retains the
  originating bar, level, acceptance definition and touch count.
- Breakout partial cashflows are credited when banked; final close credits only
  the remainder. Repeating close has no balance or learning effect. Broker-executed
  rows (`success`, legacy `executed`, or a ticket) cannot receive nominal virtual R.
- The fast Breakout route rechecks generation permission and Trading Markets,
  requires cycle context no older than 120 seconds, validates the executable quote
  and spread, and applies the candle route's volatility-scaled break floor. Starting
  a cycle invalidates old context; a failed refresh cannot keep feeding entries.
  The cycle's existing ATR-collapse check runs before its cache becomes usable.
  With timed bridge quotes, the source M5 close must also be within one M5 bar;
  receiving an old candle recently cannot make it fresh.
  Both routes repeat quote, age and generation/session eligibility after the
  asynchronous review and before creating a signal.
- Breakout fill refresh uses the same closed-M5/UTC conversion as generation.
  H1's forming range remains intentional; the historical replay still omits the
  fast route and several live gates. It cannot certify live-policy returns.
- Broker evidence history retries no longer abandon trades after a day; a rotating
  bounded queue prevents a new-trade backlog starving older partial/fee evidence.

Trend PA and Breakout append creation, dispatch-attempt, refreshed-feature and broker-result observations to
`decision_observations` in their own databases. Policy snapshots contain the
selected template contents, environment and hashes; `executable-quotes-v2`
identifies the new price convention. Reversal's existing immutable snapshots carry
that virtual-label version too. Unknown source availability is recorded explicitly.
The global strategy fallback is resolved, while an automatic choice remains marked
unresolved until its actual result is available. Breakout records the ordered
feature names and `breakout-proxies-v1` identity; equal-width vectors with different
feature orders do not share a schema hash. An attempt is never recorded as a fill.
Virtual targets and arbitrary selected templates can differ: these observations
make that difference auditable; a virtual model score is not validation of an
arbitrary broker exit policy. Historical labels remain under their original meaning.

## Three fixed offline hypotheses

Use `.venv/bin/python -m tools.signal_entry_research --help`.

```sh
.venv/bin/python -m tools.signal_entry_research --protocol-only --output /tmp/entry-protocol.json
.venv/bin/python -m tools.signal_entry_research --input /path/opportunities.json --output /tmp/entry-trial.json
```

Output files must be new. The tool never loads a production database/model,
contacts a broker, activates settings or promotes a strategy. It records protocol,
dataset and source hashes. Defaults are locked in `entry-trials-v1`; changing a
condition creates another research trial, not a retrospective improvement of this one.

| Family | Candidate eligibility before waiting for a better fill |
|---|---|
| `structural_pullback` | Fresh break, no earlier touches, trend aligned, at least 2R room |
| `breakout_retest` | Fresh break, no earlier touches, at least two acceptance closes, compression in (0, 2.5] ATR |
| `failed_continuation` | Failed continuation, spread recovery, opposing exchange flow observed within one second |

The input JSON has `opportunities` and `quotes` arrays. Each opportunity requires
`id`, `decision_ts`, `context_available_at`, `family`, `direction` (BUY/SELL), an
absolute `stop`, positive `atr`, explicit incremental `cost_pts`, and `context`.
Times are UTC seconds. Context must already have been available at decision.
Missing facts do not qualify a candidate. Context keys follow the conditions above:
`fresh_break`, `touch_count`, `trend_aligned`, `room_r`, `acceptance_closes`,
`compression_atr`, `failed_continuation`, `spread_recovered`, `flow_against_break`,
`flow_source` (`exchange`) and `flow_age_s`. Supply the keys for that family.
Each quote requires `ts`, `available_at`, `bid`, `ask`. Do not substitute tick
counts or delayed free futures bars for exchange flow. No paid feed was enabled.

Baseline enters immediately; candidate waits up to 300 seconds for 0.25 ATR
improvement. Both use the same original stop and 2R target and original offered
risk denominator. Both exit after at most one hour. There is one open or pending
position per family/variant. Filtered and missed opportunities contribute zero;
incomplete paths are censored and disqualify review. Spread is paid through the
quotes once, incremental costs once; stop gaps use the first observed exit quote.
Quote crossings approximate resting fills: queue priority and unobserved movement
between samples are unknown. A backtest cannot repair missing tick evidence.

Reports include fill rate, per-opportunity and per-fill expectancy, profit factor,
drawdown, paired improvement and day-cluster lower confidence bounds. Review needs
200 fills across 30 days, positive chronological halves, positive absolute and
paired lower bounds with Bonferroni correction for three trials, and positive
lower bounds after 0.25 point additional cost stress. The forward window begins
19 October 2026 UTC. Historical data remain exploratory. Timestamp flags alone
do not authenticate input provenance; retain arrival logs and immutable datasets.
`deployable` is always false. No qualifying real forward trial has been observed.

## Interpretation and remaining verification

ADX and MACD retain their historical proxy definitions and calibrated thresholds.
They are not interchangeable with Wilder/chart values. Canonical replacements,
session exclusions, larger models and reversed losing rules need separate trials.
The historical negative track record does not justify enabling any of these ideas.

Automated regression tests use fakes and temporary databases. An attended demo
session is still required to verify broker fills, selected-template management,
cashflows and policy attribution after deployment. There has been no broker order,
production-history rewrite, model retrain or switch activation during this work.
