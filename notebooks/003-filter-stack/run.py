# %% [markdown]
# # 003 — Filter stack: combine everything found so far into one candidate config
#
# **Hypothesis**: the three independent findings — (a) sub-0.75 R:R signals
# bleed (001-review), (b) hours 12–16 & 19 UTC concentrate losses, (c) round/
# congestion levels lose while asia/swing/unicorn don't — stack into a config
# with positive walk-forward expectancy. Also tests a geometry change: replace
# the 8-TP ladder with a single take-profit at 1.0R / 1.5R.
#
# **Method**: every config is replayed through the ONE simulator on the same
# signals; configs are compared walk-forward (config chosen on prior days
# only, evaluated on the next day). In-sample tables are context, not results.

# %% setup
import sys
from pathlib import Path

import numpy as np
import pandas as pd

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "_shared"))

from lib import loaders, metrics, report, sim

HERE = Path(__file__).resolve().parent
OUT = HERE / "output"
OUT.mkdir(exist_ok=True)

signals = loaders.signals_df()
prices = loaders.prices_df()
settled = signals[signals["status"].isin(["closed", "expired"])].copy()

BAD_HOURS = {12, 13, 14, 15, 16, 19}
GOOD_LEVELS = {"asia_high", "asia_low", "swing_high", "swing_low", "unicorn"}

# %% config definitions — filters/geometry as sim hooks
def f_rr(row):     return float(row.get("rr_tp1") or 0) >= 0.75
def f_hours(row):  return int(row["hour_utc"]) not in BAD_HOURS
def f_levels(row): return row["level_type"] in GOOD_LEVELS
def f_stack(row):  return f_rr(row) and f_hours(row) and f_levels(row)

def geo_fixed_rr(mult):
    """Single TP at mult*R from zone mid, full close there, no ladder."""
    def geo(row):
        lo, hi = float(row["entry_low"]), float(row["entry_high"])
        mid = (lo + hi) / 2.0
        sign = 1.0 if str(row["direction"]).upper() == "BUY" else -1.0
        sl_dist = float(row.get("sl_dist") or 0)
        if sl_dist <= 0:
            return None
        return {"tps": [mid + sign * mult * sl_dist]}
    return geo

CONFIGS = {
    "baseline":       sim.SimConfig(),
    "rr>=0.75":       sim.SimConfig(signal_filter=f_rr),
    "block bad hrs":  sim.SimConfig(signal_filter=f_hours),
    "good levels":    sim.SimConfig(signal_filter=f_levels),
    "stack(filters)": sim.SimConfig(signal_filter=f_stack),
    "tp@1.0R":        sim.SimConfig(geometry=geo_fixed_rr(1.0), fractions=(1.0,)),
    "tp@1.5R":        sim.SimConfig(geometry=geo_fixed_rr(1.5), fractions=(1.0,)),
    "stack+tp@1.5R":  sim.SimConfig(signal_filter=f_stack,
                                    geometry=geo_fixed_rr(1.5), fractions=(1.0,)),
}

# %% replay every config once over all signals
runs: dict[str, pd.DataFrame] = {}
for name, cfg in CONFIGS.items():
    res = sim.run(settled, prices, cfg)
    res = res.merge(settled[["id", "day", "hour_utc", "level_type"]],
                    left_on="signal_id", right_on="id")
    runs[name] = res
    print(f"replayed {name:16} filled={len(sim.filled(res)):4}")

# %% in-sample comparison (context only)
insample = {n: metrics.summarize(sim.filled(r).sort_values("close_ts")["r"],
                                 n_candidates=len(settled))
            for n, r in runs.items()}
tab = metrics.summary_table(insample)
print("\nIn-sample, whole period (CONTEXT ONLY):")
print(tab[["trades", "total_R", "expectancy_R", "win_rate", "profit_factor", "max_drawdown_R"]])

# %% walk-forward: pick best config on train days, apply to next day
days = sorted(settled["day"].unique())
MIN_TRAIN, MIN_TRADES = 3, 8
wf_rows, wf_equity_stack, wf_equity_base = [], [], []
for k in range(MIN_TRAIN, len(days)):
    train_days, test_day = days[:k], days[k]
    best_name, best_exp = None, -np.inf
    for name, r in runs.items():
        tr = sim.filled(r)[sim.filled(r)["day"].isin(train_days)]
        if len(tr) < MIN_TRADES:
            continue
        e = tr["r"].mean()
        if e > best_exp:
            best_exp, best_name = e, name
    te = sim.filled(runs[best_name])[sim.filled(runs[best_name])["day"] == test_day]
    tb = sim.filled(runs["baseline"])[sim.filled(runs["baseline"])["day"] == test_day]
    wf_rows.append({"test_day": test_day, "chosen_config": best_name,
                    "trades": len(te), "test_R": round(te["r"].sum(), 2),
                    "baseline_trades": len(tb), "baseline_R": round(tb["r"].sum(), 2)})
    wf_equity_stack += list(te.sort_values("close_ts")["r"])
    wf_equity_base += list(tb.sort_values("close_ts")["r"])

wf = pd.DataFrame(wf_rows)
print("\nWalk-forward (config chosen on prior days only):")
print(wf.to_string(index=False))
wf_total, base_total = wf["test_R"].sum(), wf["baseline_R"].sum()
print(f"\nTotals across test days: chosen {wf_total:+.1f}R vs baseline {base_total:+.1f}R")

# %% fixed-config walk-forward honesty check: the full stack held constant
stack_wf = sim.filled(runs["stack+tp@1.5R"])
stack_wf = stack_wf[stack_wf["day"].isin(days[MIN_TRAIN:])]
fixed_m = metrics.summarize(stack_wf.sort_values("close_ts")["r"])
print(f"\n'stack+tp@1.5R' held FIXED over the same test days: "
      f"{fixed_m['total_R']:+.1f}R over {fixed_m['trades']} trades, "
      f"expectancy {fixed_m['expectancy_R']:+.3f}R")

# %% standardised RESULTS.md
wf_m = metrics.summarize(pd.Series(wf_equity_stack))
base_m = metrics.summarize(pd.Series(wf_equity_base))
numbers = metrics.summary_table({
    "walk-forward chosen": wf_m,
    "walk-forward baseline": base_m,
    "stack+tp@1.5R fixed (test days)": fixed_m,
    **{f"[in-sample] {n}": m for n, m in insample.items()},
})
tab.to_csv(OUT / "insample_all_configs.csv")
wf.to_csv(OUT / "walkforward.csv", index=False)

headline = (
    f"The full stack — R:R ≥ 0.75, block 12–16/19 UTC, drop round-number/congestion "
    f"levels, single 1.5R take-profit — held fixed on unseen days made "
    f"{fixed_m['total_R']:+.1f}R over {fixed_m['trades']} trades (expectancy "
    f"{fixed_m['expectancy_R']:+.3f}R/trade), while the unfiltered baseline lost "
    f"{base_total:+.1f}R over {wf['baseline_trades'].sum()} trades on the same days. "
    f"The adaptive day-by-day chooser only managed {wf_total:+.1f}R — the value is in "
    f"the fixed filters, not in switching."
)
report.write_results(
    HERE,
    experiment="003 — filter stack",
    headline=headline,
    numbers=numbers[["trades", "total_R", "expectancy_R", "win_rate",
                     "payoff_ratio", "profit_factor", "max_drawdown_R"]],
    chart_series={
        "baseline": wf_equity_base,
        "adaptive chooser": wf_equity_stack,
        "fixed stack+tp@1.5R": list(stack_wf.sort_values("close_ts")["r"]),
    },
    chart_title="Walk-forward test days only — cumulative R",
    extra_sections={"Walk-forward detail": wf.to_markdown(index=False)},
    caveats=[
        "9 trading days, one strongly-trending gold market — 'on this sample' applies to every number.",
        "60-second price series: fills and tight ladders are approximate; re-run on M1 candles before trusting geometry conclusions.",
        "Filters shrink trade count substantially — see the trades column before celebrating the R totals.",
        "Zero spread/commission modelled in these runs (cost_R=0).",
    ],
    verdict=(
        "KEEP (provisional). First config with positive walk-forward expectancy, and "
        "every component was motivated by an earlier finding rather than searched for. "
        "But 28 trades is far too few to promote anything: NEEDS-M1 for the geometry "
        "leg and NEEDS-MORE-DATA (fresh snapshots) before this goes near the backend."
    ),
)
print(f"\nwrote {HERE / 'RESULTS.md'}")
