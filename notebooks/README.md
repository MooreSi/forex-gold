# The Lab — signal testing, away from the real system

**Hi Simon.** This folder is your sandbox for testing ideas against the
trading data — completely separate from the live app. Nothing in here can
place a trade, touch MT5, or change the backend. Break anything you like.

## The one rule

**Prove it here first.** Don't change the backend's ML or trading logic on a
hunch. Test the idea in this folder against the recorded data. If a test
shows genuinely positive results — across different days, not just one lucky
afternoon — *then* ask the AI agent to promote it into the main system
(properly versioned, with Simon/Darren sign-off on anything money-related).

## How to use it

- Each numbered folder (`001-...`, `002-...`) is one experiment: one
  question, one answer. Read its `README.md` — **Hypothesis → Method →
  Result** — that's the whole story, no code-reading needed.
- Each experiment also has a **`RESULTS.md`** — the same fixed layout every
  time: one headline sentence, a numbers table, a chart, the caveats, and a
  verdict (KEEP / DROP / needs better data). If you only read one file per
  experiment, read that one. It's regenerated automatically each run, so
  it's never out of date.
- Want to test a new idea? Just tell the AI agent, e.g. *"new experiment:
  what happens if we only trade London hours?"* It will create the next
  numbered folder and run it.
- To re-run an experiment yourself: `python 001-baseline-replay/run.py`
  from this folder (plain Python, results print to screen and save to
  `output/`).
- Fresh data makes every experiment smarter. Drop new database snapshots
  into `_shared/data/` (dated filenames) and ask the agent to re-run.

## What's been found so far

| # | Question | Answer |
|---|---|---|
| 001 | Can we replay history accurately offline? | Yes — 85% agreement with the recorded outcomes. Also found that the stored stop-loss column is rewritten after trades — trap for the unwary. |
| 002 | Is the engine's ML score helping? | **It's backwards.** It scores losing signals *higher* than winners, and the live gate built on it is filtering out winners. The features it learns from are the problem. |
| 003 | Do the three fixes stack into a winner? | **First positive result.** Require decent reward-vs-risk, skip the bad hours, skip round-number levels, take profit at 1.5× risk: +10R on days the config never saw, while the unfiltered engine lost 20R. Only 28 trades though — promising, not proven. |

## House rules (the agent enforces these — see CLAUDE.md)

1. Experiments read data copies in `_shared/data/` only — never the live
   app, never MT5, never real money.
2. Results always report both "how much better" **and** "how many trades
   were left" — an idea that wins by barely ever trading isn't a win.
3. Failed experiments stay. Knowing what *doesn't* work is half the sauce.
