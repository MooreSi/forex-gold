# Making a constant configurable

There are nine places a setting can live in this app. Picking the wrong one is
how ~135 behaviour constants ended up hardcoded in the first place. This page
is the decision rule.

## Which tier does it belong in?

| Tier | Storage | Use it for | Synced? |
|---|---|---|---|
| 1 | `config.yaml` | machine identity & bootstrap: paths, API keys, bridge URL — things needed *before* the database opens | no |
| 2 | `vantage_risk_settings` | trading behaviour the user flips: sizing, limits, strategy, toggles | yes |
| 3 | `app_config` | operational state: pause-until, last-email markers, sync wiring | mostly no |
| 4 | **`EXPERT_PARAMS`** | **behaviour constants a trader might tune** | yes |
| 5 | Strategy Parameters | per-strategy SL/TP shaping | yes |
| 6 | Adaptive params | per-engine values the AI tuner may adjust | per engine |

**Never trading behaviour in tier 1.** It is per-machine and never syncs, so a
paired Mac and VPS would trade differently and nothing would say why.

## The default rule — non-negotiable

> **The default must be byte-identical to the constant it replaces.**

Installing a new tunable must not change how the system trades. Nothing moves
until a human moves a dial. A release that silently retunes the strategy is
worse than the hardcoded value was.

This is asserted per-parameter in `tests/core/test_expert_params.py`. Adding a
parameter without adding it there will not be caught by anything else.

## Adding an Expert Tunable

**1. Add the spec** — `backend/src/services/risk/expert_params.py`:

```python
ExpertParam(
    key="max_signal_age_s", label="Maximum signal age", default=240,
    min=30, max=3600, unit="s", domain="Signal handling", integer=True,
    desc="Signals older than this at processing time are recorded as "
         "historical and never executed. Raised carelessly, this is how a "
         "backfilled signal fills minutes late at a worse price.",
),
```

`desc` explains **the effect and the risk**, not the name. It is what the
user reads before changing something that can cost money.

Pick `min`/`max` so that every value in the range is *survivable*. The clamp
is a safety control: a 0 in the R:R floor would open trades the system
currently refuses.

**2. Replace the constant with an accessor** — never read `expert_params.get()`
at import time, or the value freezes at startup:

```python
def max_signal_age_secs() -> int:
    """Was a 4-minute constant; now Settings > Expert Tunables."""
    return expert_params.get("max_signal_age_s")
```

**3. Add both tests.** Both, always:

```python
def test_the_default_signal_age_cutoff_is_still_240s():
    assert scan_staleness.max_signal_age_secs() == 240        # upgrade safety

def test_the_signal_age_cutoff_follows_the_catalogue():
    ep.set_params({"max_signal_age_s": 600})
    assert scan_staleness.max_signal_age_secs() == 600        # actually wired
```

The first alone lets you ship a control wired to nothing. The second alone
lets the default drift. Together they pin the value *and* the connection.

**4. Add the default to `EXPECTED_DEFAULTS`** in
`tests/core/test_expert_params.py`.

**5. The UI needs no work.** `frontend/pages/expert_tunables.py` renders the
catalogue generically. That is the entire point — a new tunable costs one
catalogue entry.

## What NOT to expose

Roughly 135 constants are hardcoded. Most should stay that way:

- **Protocol values, display constants, contract size, broker TZ offsets** —
  not tunable, just facts.
- **Engine calibration** — ADX/ATR band edges, swing lookbacks, ML retrain
  cadence. Their interactions are only verified by the test suite; exposing
  them makes the safe envelope meaningless.

Expose a constant when a *trader* would want to move it, not when a
*developer* notices it is a magic number.

## Reference

- Full inventory and tiering: `docs/todo/refactor/stage0/CONFIG_AUDIT.md`
- Implementation: `backend/src/services/risk/expert_params.py`
- Tests: `tests/core/test_expert_params*.py`
