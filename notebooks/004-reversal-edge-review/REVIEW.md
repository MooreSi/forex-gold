# Reversal engine: engineering and trading review

7 October 2026. Research and an offline prototype only; no production code, trading settings, model files, EA or orders changed.

## Decision

**The current reversal engine has no demonstrated positive trading edge. A larger ML model is not the next justified change.** The evidence points first to an economic problem in the setups/exit policy, compounded by inconsistent labels, feature defects and insufficient deployment validation. Fixing correctness is necessary, but does not itself create alpha.

My recommendation is to review two separate pieces of work: (1) make the data and decisions trustworthy; (2) conduct a bounded search for a new source of predictive information. Promote a trading change only after the second piece passes an independently frozen evaluation. The prototype here is a research baseline and a validation harness, not a profitable replacement bot.

## What I inspected

- Generator, level detector, ML feature extraction, training labels, batch and online updates, candidate ranking, fill-time rescoring, virtual management and live gates.
- Reference-pattern model/profile, meta-labeller, template labelling, proven-edge model and time-series validation.
- Earlier lab results and the recorded 2025 / 2019–2024 second-vendor studies.
- A selective, read-only snapshot of **7,278 signals**, **6,165 closed signals**, **68,291 analysis records**, **11,886 balance entries**, **3,930 shadow decisions**, and **84 cross-asset fits**. Signal dates: 23 July–7 October 2026.
- The demo consolidated ledger and reversal template settings, plus relevant events in the two current text logs. The logs duplicate many events; counts are not added across files.

Snapshot captured at **14:25:16 BST**, 7 October. Sources were opened with SQLite `mode=ro`, in read transactions. Only whitelisted research tables were copied; no credential table contents were read. SHA-256, table counts, feature names and code revision are in [manifest](output/manifest.json). No service was started and no broker API was called.

The checkout already had unrelated edits. They were preserved. Current code and the snapshot are an audit boundary, not proof that every historical row was produced by this exact code revision.

## What the wins and losses actually say

R means profit divided by the trade's initial risk. These are three different populations and cannot be pooled:

| Population | Observations | Result | Meaning |
| --- | ---: | --- | --- |
| Closed virtual signals with valid net-risk labels | 5,203 | **−439.55R**, −0.0845R/trade, 74.5% positive returns, profit factor 0.749 | Historical simulator/virtual management, not broker execution |
| Stored replay labels for the old fixed EA template | 6,133 | **−899.64R**, −0.1467R/trade, profit factor 0.721 | M1 replay with old 5-point stop and 0.575-point assumed execution cost |
| Closed signals marked executed | 962 | Recorded net dollars sum **−$2,975.27** | Broker-close values recorded in the signal table; no valid common R denominator here |

Virtual profitable trades average **+0.338R**, negative trades **−1.320R**. Ignoring approximately flat outcomes, that payoff requires about **79.6%** winners to break even, versus the observed 74.5%. A high headline win rate is not a sufficient edge.

The demo consolidated ledger contains 2,574 reversal entries, but only 299 distinct non-null tickets, and includes virtual records and records from two nodes. Its raw −$11,387.10 is **not** a verified broker-account loss. A stable-reference join matches 217 executed signal records, totaling −$581.84, with realized R available for 205 (−9.8205R). That is incomplete coverage of the 962 executed records, so I do not extrapolate it to an account statement. Full broker-deal reconciliation remains outstanding.

Old-template replay expectancy is negative in July (−0.131R), August (−0.159R) and September (−0.138R). Every material level-type and session group is negative. Unicorn is closest to flat at −0.022R over 143 labels; that is an exploratory observation, not a promotion candidate. October has only **two** stored template labels in this snapshot. There are no new signal records dated 1–6 October; this audit cannot infer why from the rows alone.

## The actual system is a rules engine with ML selection

The detector proposes Asia extremes, swings, round numbers, congestion and selected structure setups. Rule scores, bias checks, proximity and other gates establish eligibility; the ML model ranks a small eligible candidate pool. Creation-time features and expected R are saved. At a later touch, the live path recalculates some context, runs several gates and applies the EA strategy/template. Virtual signals continue producing outcomes even when execution is blocked.

Production v9 is a LightGBM R regressor with 100 trees and leaves allowed to contain five rows, blended **60% batch / 40% online Huber SGD**. It retrains every five trainable closes. `ml_prob` is a legacy name: **it is expected R, not a probability**. The meta-labeller predicts positive net virtual return; pro-likeness predicts resemblance to reference entries; the proven-edge model predicts old-template R. These are different targets.

## Confirmed defects and design gaps

### 1. Asia range is not the most recent completed session

[level_detector.py:69](/Users/simon/Forex-Gold/backend/src/services/reversal_engine/level_detector.py:69) keeps every candle whose UTC hour is below eight, across the full input. The caller supplies 50 H1 candles, so multiple Asia sessions can be mixed; a current incomplete session can be included too.

An exact source-fragment reproduction gives **100–210** for two sessions with ranges 100–110 and 200–210. The most recent completed session should be **200–210**. This changes the support/resistance levels before ML even sees them. Proposed correction: group by UTC session date, require completion as of decision time, select the latest eligible session, and make stale/partial coverage explicit. Profit impact has not been measured.

### 2. A genuine zero FVG distance is still corrupted in pro-likeness

[pro_model.py:132](/Users/simon/Forex-Gold/backend/src/services/reversal_engine/pro_model.py:132) reads `distance or 5.0`. A zero distance, meaning directly at the gap, becomes five ATR away. The primary ML extractor was repaired, but this second model still has the defect. Reproduced with its exact `_vector` source fragment. Proposed correction: distinguish `None` from zero consistently, then version/rebuild affected pattern models and audit their stored corpus.

### 3. Training, fill scoring and meta scoring use different feature information

Training reads stored creation-time vectors. The proven-edge execution decision uses freshly recomputed fill-time vectors. **Those fill vectors are not stored**, only the fill score and bias, so an exact fill-model backtest cannot currently be reconstructed.

Additionally, [live_execute.py:345](/Users/simon/Forex-Gold/backend/src/services/reversal_engine/reversal_engine_live_execute.py:345) passes the original database signal to `meta_label.score_signal`, which re-extracts features at [meta_label.py:319](/Users/simon/Forex-Gold/backend/src/services/reversal_engine/meta_label.py:319). That row does not hold the transient RSI/FVG/macro context supplied during generation. Training sees enriched stored values, but this inference path substitutes defaults for several inputs. It also combines old signal fields with current outcome statistics. Proposed correction: score all enabled models from a single explicitly timed decision snapshot; separately train creation and fill models when both decisions exist.

### 4. Labels do not follow today's template

The production regressor learns the virtual 0.1-lot management result. Executed rows are deliberately excluded because their true lot and initial risk are absent; this recent exclusion is correct and should be retained.

`tpl_r` is better aligned to an executable trade, but [entry_study.py:142](/Users/simon/Forex-Gold/backend/src/services/reversal_engine/entry_study.py:142) hardcodes the old fixed template, approximating parts of its ladder as a runner. [tpl_label.py:32](/Users/simon/Forex-Gold/backend/src/services/reversal_engine/tpl_label.py:32) fixes costs at 0.575 points. Labels have no policy hash or explicit replay label-availability timestamp. They cannot prove an edge for today's dynamic `Reversal ATR v1` template.

The snapshot has generator ATR barriers **off**, live execution **on**, meta execution gate **off**, and proven-edge gate **off**. Today's logs contain **nine** template refusals because generated TP1:SL ratios of 0.43/0.60/0.75 fail the selected template's minimum 0.90. There was also an EA version refusal (chart 1.09 versus shipped 1.10) and a circuit-breaker refusal. These are execution/configuration issues, not proof of ML failure. Proposed correction: freeze one target policy, validate compatibility before generating signals, and label that exact version.

### 5. Thousands of training rows collapse to roughly 118 influential observations

[ml_engine:392](/Users/simon/Forex-Gold/backend/src/services/reversal_engine/ml_engine/__init__.py:392) uses `exp(−0.017 × row_age)`. Half-life is **40.8 rows**. Effective weighted sample size, `(sum w)^2/sum(w^2)`, is **117.65**, before accounting for overlapping market moves. Decay uses creation-ID order rather than elapsed calendar time or outcome availability. The normalized weights preserve the same effective sample size.

Five-row leaves, frequent refits and an unvalidated 40% online blend make it easy to chase a recent move. These are model-design weaknesses, not syntax bugs. Proposed correction: compare calendar-based decay and larger leaves in nested walk-forward research; require the online component to beat the same locked batch baseline after costs before blending it.

### 6. Purged cross-validation is not a live forward test

The meta-labeller uses `purged_kfold`. It removes overlapping labels but retains **future** training blocks when testing earlier blocks. An exact reproduction trains on indices 2–7 while testing 0–1. This is valid for some retrospective cross-validation questions, but its AUC is not evidence that the same model could have been deployed at the test time.

Today's meta logs report AUC **0.576** with peers versus **0.578** without, on the same 5,203 rows. That does not establish positive expectancy, probability calibration, or an incremental cross-asset benefit. The proven-edge model already uses forward splits and an abstention policy: its captured status is **−0.1769R** on 422 accepted test labels, t=−4.0, AUC=0.5069, with both chronological halves losing. Its implementation is useful protection; switching it on is a separate trading-policy decision for review.

### 7. Missing context and model health are not sufficiently explicit

Historical news proximity is at its modal value **97.2%** of the time, and equity drawdown is constant on all 7,278 vectors. Several missing inputs become plausible neutral observations. Prediction exceptions can be swallowed and return `None`; ordinary ML gates allow execution when there is no score. Training acceptance requires as few as 15 labels and no production out-of-sample profitability test. Proposed correction: record missingness, source age, feature-schema and model IDs; emit distinguishable health states; make deployment eligibility depend on demonstrated cost-adjusted returns.

Candidate ranking also learns only from the winning candidate that was saved. Alternatives in the same pool are not all recorded and labelled, so the present history cannot directly establish whether ML picked the best alternative. Future research needs candidate-level logs and counterfactual labels, not an assumption that more ranking capacity helps.

## Additional risks requiring validation

- **Hourly macro look-ahead:** `macro_backfill._closes_at` chooses closes stamped at or before decision time. If the vendor stamps an hourly bar at its opening time, its closing price is available only an hour later. The source-fragment demonstration confirms the lookup behavior, but vendor timestamp/publication semantics were not revalidated here. Treat this as a conditional risk, not a confirmed end-to-end leakage finding. Exclude reconstructed macro inputs until the availability contract is verified.
- Current candle completion, news-history reconstruction and precise bid/ask intrabar replay need a dedicated data audit. This review did not fetch fresh M1/ticks or rerun every multi-year simulation.
- The established lab metric's drawdown starts at the first cumulative return rather than initial zero equity. It can understate a losing first trade. The standard table is preserved; corrected companion drawdowns are in [audit.json](output/audit.json). The tree's true drawdown is −10.713R, versus −9.60R in the shared table.

## Prototype and results

[run.py](run.py) implements two fixed research challengers: standardized ridge regression and a shallow LightGBM with 100-row leaves. Both use an observed market/clock subset, excluding account bookkeeping, channel imitation and reconstructed macro. Both predict **old-template net R**, accepting only expected R above +0.05. Historical range-regime substitutions are repaired where row ADX proves the value.

Training expands through earlier whole days. Each training outcome must be available before the test day minus a one-hour gap; availability is conservatively the later of actual close and trigger + six-hour template horizon. There are **3,913 test rows over 36 days**, 20 August–7 October. Tests are retrospective; the data and earlier research have already been inspected, so this is not a pristine holdout. Selection/model settings were fixed before seeing these challenger results.

| Selection on identical offered test rows | Trades / 3,913 | Mean net R | Profit factor | Result |
| --- | ---: | ---: | ---: | --- |
| Unfiltered old-template labels | 3,913 | −0.1543 | 0.709 | Losing baseline |
| Logged creation expected R ≥ 0 | 2,196 | −0.1775 | 0.674 | Worse expectancy; not an exact replay of fill-time live gates |
| Ridge expected R > 0.05 | 13 | −0.3346 | 0.452 | No edge; extremely low coverage |
| Shallow tree expected R > 0.05 | 24 | −0.4464 | 0.359 | No edge; extremely low coverage |
| Shuffled TRAIN labels, same ridge/threshold | 15 | +0.3973 | 2.782 | Noise control; **not a candidate** |

Ridge and tree ranking AUCs are **0.505** and **0.501**, essentially chance. Logged creation scores rank old-template profitable outcomes at **0.487**. Day-bootstrap 95% intervals for mean R are −0.1740 to −0.1349 for the baseline and −0.2074 to −0.1485 for logged positive scores. These are descriptive clustered intervals, not multiple-testing-adjusted deployment proofs; longer serial dependence is still possible.

The shuffled control's apparent success comes from four days and its interval crosses zero (−0.1732 to +0.5828). Its all-score AUC is 0.496. It demonstrates that a positive tiny selected sample can occur after destroying the training relationship. Adding another 0.25 point execution cost makes every genuine challenger worse. Full metric suite, trade coverage, stress results and charts: [RESULTS](RESULTS.md).

Earlier repository studies also matter: the recorded 2025 second-vendor level-touch/template replay lost **−0.117R/trade over 27,065 trades**. The subsequent 2019–2024 tests rejected all three nominated alternative hypotheses. I read their saved reports and code, but did **not** rerun those large studies. They reduce the case for simply rescaling targets or changing model architecture; they do not prove that every possible gold strategy is impossible.

## What credible external ML trading research contributes

I found credible examples of **historical out-of-sample** ML investment results, not independently audited proof of a deployable retail XAUUSD reversal bot. The transferable lessons are specific:

1. **Economic features and regularization.** Gu, Kelly and Xiu find useful nonlinear interactions among momentum, liquidity and volatility in a large equity panel, and compare regularized methods over decades. This is equity risk-premium research at a different horizon; its performance cannot be transferred to intraday gold. The applicable lesson is to define informative market measurements and compare simple baselines before adding capacity. [Authors' paper](https://dachxiu.chicagobooth.edu/download/ML_BKP.pdf).
2. **Model a source of relative mispricing.** Guijarro-Ordonez, Pelger and Zanotti first form residual portfolios of similar assets, then learn temporal signals and a constrained trading policy. Their reported out-of-sample daily-equity results support a residual/economic-mechanism approach, not a claim that a transformer can rescue arbitrary support/resistance entries. For gold, a cross-asset residual hypothesis would need its own stable relationship and independent validation. [Research paper](https://arxiv.org/abs/2106.04028).
3. **Treat searching as a source of false discoveries.** Bailey and coauthors show why selecting the best backtest can overfit and develop a framework for estimating that risk. Keep a trial ledger, test alternatives only on training/validation, and reserve a final fresh period. The positive shuffled control here is a practical warning against choosing a tiny best-looking result. [Authors' paper](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf).
4. **Gold reacts to scheduled macro information.** Roache and Rossi's event study identifies gold responses to particular US and euro-area announcements. It motivates point-in-time event/regime features, not an automatic profitable fade around news. The study is historical, so current effect size must be re-estimated. [IMF working paper](https://www.imf.org/-/media/websites/imf/imported-full-text-pdf/external/pubs/ft/wp/2009/_wp09140.pdf).
5. **Time causality is an implementation contract.** Scikit-learn documents expanding temporal splits and a train/test gap. For this bot, a sample gap alone is insufficient: delayed labels must also be purged using their actual availability time. [Official documentation](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html).

I would not prioritize an LSTM, reinforcement learning or an LLM direction judge on the present selected-signal dataset. That is an engineering recommendation based on the current data and failed baselines, not a universal claim about those model classes.

## Proposed next engine, for review

**First freeze the contract:** exact entry method, one exit policy, initial risk, costs, label horizon and timeout behavior. No silent template changes. Each label needs policy hash, feature schema, decision timestamp, feature-source timestamps, actual fill, initial stop/risk and label-available timestamp. Preserve positive/negative/expired/blocked candidates and all alternatives in a ranking pool.

**Then test one mechanism at a time.** A reversal candidate should represent measurable excess/failed continuation, rather than proximity alone: volatility-normalized stretch from a session anchor, approach speed, rejection/reclaim, repeated level tests, trend persistence and event context. Record separate candidate and confirmation times. A sequence such as approach → sweep → reclaim → entry is a hypothesis only: the previous generic sweep-fade already failed, so adding a pattern name is not evidence. Spot broker tick volume is not centralized COMEX traded volume.

Possible richer hypotheses are a point-in-time gold/currency/rates residual and the persistence/reversal of that residual in distinct volatility regimes. The current peer additions did not improve the reported classification AUC, so justify any new relationship economically and test incremental net value on the same rows. Do not assume more markets automatically add edge.

**Use the model to estimate executable value and uncertainty.** Begin with linear and shallow-tree baselines. Compare direct net-R regression with a model estimating positive-return probability plus conditional gain/loss magnitudes. The decision is expected net payoff, not `P(win)>0.5`. A probability model with rare large losses can still be economically wrong. If labels are already net, do not subtract execution costs twice; reserve an additional explicit stress margin for deployment testing. Require enough independently clustered accepted observations and refuse to arm when evidence is insufficient.

**Separate selection quality from management quality.** Compare entry hypotheses under one frozen exit policy, then compare exits on the same entries. Report net expectancy, payoff ratio, coverage, tail loss, drawdown, calibration, latency and cost stress. Reconstruct exact fills for the promotion candidate on bid/ask ticks or conservative M1 bounds; validate rejected alternatives without pretending they were broker fills.

**Deployment evaluation:** nested expanding walk-forward with actual outcome availability; day/week clustering; a registered finite trial list; research-selection correction; cross-vendor/multi-year testing; finally a genuinely new frozen forward period. Only after that should a shadow challenger be connected to the running app, followed by separately approved demo evaluation. An adaptive online learner is a challenger until it independently improves the frozen batch baseline.

Suggested review order: (1) agree the label/template and decision logging contract; (2) approve isolated correctness fixes and regression cases; (3) approve a small hypothesis list and untouched future evaluation window; (4) consider production integration only when those results support it. No thresholds, trade pauses, gate toggles or sizing changes have been applied here.

## Verification and limits

- Five isolated prototype checks pass: future/unresolved labels excluded, broker dollars not treated as virtual R, net costs not charged twice, invalid labels rejected, invalid/weak scores abstain. Written and run failing before implementation.
- Four source-fragment demonstrations pass their expected-behavior assertions; these document current defects/risks and do not patch them.
- **79 existing reversal tests pass** covering labels, training rows, valid zeros, candidate ranking, fill-time rescoring and proven-edge logic. Passing tests do not disprove the untested defects above.
- Experiment completes and regenerates its standard report, CSVs and charts using the project Python environment. No runtime dependency was installed. The report table uses a local adapter because optional `tabulate` is absent.
- No full repository suite, broker connection, fresh price-data replay, live-account statement audit or deployment was performed. Production files are unchanged; there is no claim of a profitable replacement strategy.
