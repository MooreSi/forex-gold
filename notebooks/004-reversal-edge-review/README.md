# 004 — Does cleaner ML establish an executable reversal edge?

## Hypothesis

Matching labels to a fixed executable policy and using simpler models with strictly earlier, resolved training outcomes might improve gold reversal selection. This is research only; no engine integration.

## Data used

Selective read-only snapshot on 7 October 2026, 14:25:16 BST: 7,278 signals dated 23 July–7 October, 5,203 valid closed virtual labels, and 6,133 stored old-template labels. Snapshot path/hash and selected demo settings are in `output/manifest.json`. Price source: recorded virtual outcomes and stored M1 replay labels; no fresh replay. Template labels use the old fixed EA template, not today's dynamic ATR template.

## Method

Compare a strongly regularized linear model and a shallow tree on market/clock features. Expand training through earlier whole days; require outcomes to be available at least one hour before the test day begins. A fixed +0.05R acceptance margin, a shuffled-training-label negative control, day-bootstrap intervals, and +0.25-point cost stress are included. Logged positive creation scores are an observational comparator, not a complete fill-time live-gate replay.

Run from the project root:

```sh
.venv/bin/python -m unittest discover -s notebooks/004-reversal-edge-review -p 'test_prototype.py'
.venv/bin/python notebooks/004-reversal-edge-review/reproduce.py
.venv/bin/python notebooks/004-reversal-edge-review/run.py
```

To capture a fresh local snapshot, `capture.py --data-dir PATH` opens only whitelisted SQLite tables read-only and creates a newly dated snapshot; it never overwrites old ones. The experiment requires the local snapshot referenced by its manifest.

## Result

**7 October 2026: no deployable edge found.** On 3,913 later test rows across 36 days, the baseline loses −0.1543R per trade. Positive logged creation scores lose −0.1775R over 2,196 trades. The linear challenger accepts 13 trades (−0.3346R/trade, profit factor 0.452); the tree accepts 24 (−0.4464R/trade, profit factor 0.359). Ranking accuracy is essentially chance. The shuffled control happens to win on 15 trades, but its day-bootstrap interval includes zero. This is evidence against promoting tiny selected samples.

The [standard results](RESULTS.md) contain the full metric suite and charts. The [engineering/trading review](REVIEW.md) contains code defects, log findings, external primary research, limitations and the proposed next engine. This study does not validate the current dynamic template or demonstrate that all possible gold strategies fail.

## Verdict

NEEDS-MORE-DATA: correct data/feature contracts and freeze the target policy before seeking a new edge. Prototype remains offline.
