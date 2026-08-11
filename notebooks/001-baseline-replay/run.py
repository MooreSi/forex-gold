# %% [markdown]
# # 001 — Baseline replay: can the simulator reproduce recorded history?
#
# **Hypothesis**: replaying the 741 recorded signals through _shared/lib/sim.py
# with the engine's own rules (2h expiry, 80/10/5/5 ladder, BE after TP1)
# reproduces the recorded fates (win/loss/expired) and the shape of the P&L.
#
# **Why it matters**: until the simulator can reproduce the past it has no
# authority over the future. Every later experiment inherits this calibration.
#
# **Data**: reversal_engine.db snapshot (21–31 Jul 2026), 60-second price
# series from re_analysis_log (directional precision only).

# %% setup
import sys
from pathlib import Path

import pandas as pd

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "_shared"))

from lib import loaders, metrics, report, sim

OUT = Path(__file__).resolve().parent / "output"
OUT.mkdir(exist_ok=True)

signals = loaders.signals_df()
prices = loaders.prices_df()
closed = signals[signals["status"].isin(["closed", "expired"])].copy()
print(f"{len(signals)} signals, {len(closed)} settled, {len(prices)} price samples")

# %% run the replay with engine-equivalent settings
cfg = sim.SimConfig()  # defaults = engine behaviour as observed in the data
res = sim.run(closed, prices, cfg)
merged = closed.merge(res, left_on="id", right_on="signal_id", suffixes=("", "_sim"))

# %% fate agreement: simulated vs recorded
def recorded_fate(row):
    return {"win": "win", "loss": "loss", "be": "be", "expired": "expired"}.get(row["outcome"], "?")

merged["rec_fate"] = merged.apply(recorded_fate, axis=1)
confusion = pd.crosstab(merged["rec_fate"], merged["fate"], margins=True)
print("\nConfusion (rows=recorded, cols=simulated):")
print(confusion)

# binary agreement on the decisions that matter (win vs loss among both-settled)
both = merged[merged["rec_fate"].isin(["win", "loss"]) & merged["fate"].isin(["win", "loss"])]
agree = (both["rec_fate"] == both["fate"]).mean()
print(f"\nwin/loss agreement where both settled: {agree:.1%} on {len(both)} signals")

# fill-rate agreement
rec_filled = (merged["rec_fate"] != "expired").mean()
sim_filled = (merged["fate"] != "expired").mean()
print(f"fill rate: recorded {rec_filled:.1%} vs simulated {sim_filled:.1%}")

# %% P&L shape: recorded dollars vs simulated R
# recorded R: net_pnl / 50 (sl_risk_usd) is only right for risk-sized strategies,
# so compare *shape* (sign, ranking), not magnitude.
both_r = merged.dropna(subset=["net_pnl_dollars", "r"])
both_r = both_r[both_r["rec_fate"].isin(["win", "loss", "be"])]
sign_agree = (
    (both_r["net_pnl_dollars"] > 0) == (both_r["r"] > 0)
).mean()
corr = both_r["net_pnl_dollars"].corr(both_r["r"], method="spearman")
print(f"P&L sign agreement: {sign_agree:.1%} | Spearman rank corr: {corr:.3f}")

# %% the headline: does the replayed book lose money like the real one did?
m = metrics.summarize(sim.filled(res)["r"], n_candidates=len(closed))
print("\nSimulated baseline metrics (R units, 1R ~= $50):")
for k, v in m.items():
    print(f"  {k:16} {v}")
print(f"\nRecorded book for comparison: -$2,855 net over 614 closed"
      f" (~{-2855/50:.0f}R), 70% win rate, avg loss 3x avg win")

# %% persist + standardised RESULTS.md
merged[["id", "signal_ref", "day", "rec_fate", "fate", "net_pnl_dollars", "r",
        "max_tp_hit", "max_tp_hit_sim", "level_type", "hour_utc"]].to_csv(
    OUT / "replay_vs_recorded.csv", index=False)
pd.Series(m).to_csv(OUT / "baseline_metrics.csv")

sim_seq = sim.filled(res).sort_values("close_ts")["r"]
rec_seq = (merged[merged["rec_fate"].isin(["win", "loss", "be"])]
           .sort_values("close_time")["net_pnl_dollars"] / 50.0)
report.write_results(
    Path(__file__).resolve().parent,
    experiment="001 — baseline replay (simulator calibration)",
    headline=(
        f"The offline simulator reproduces recorded history well enough to rank ideas: "
        f"{agree:.0%} win/loss agreement, {sign_agree:.0%} P&L sign agreement, and the "
        f"replayed book loses money like the real one did "
        f"({m['total_R']:+.1f}R simulated vs ≈−57R recorded)."
    ),
    numbers=metrics.summary_table({
        "simulated replay": m,
        "recorded book": metrics.summarize(rec_seq, n_candidates=len(closed)),
    }),
    chart_series={"recorded": list(rec_seq), "simulated": list(sim_seq)},
    chart_title="Recorded vs simulated equity — same signals, cumulative R",
    caveats=[
        "60s price series misses ~11% of fills (recorded 83.5% vs simulated 72.8%).",
        "Zero costs modelled — half the recorded loss magnitude is costs/slippage/live divergence.",
        "stop_loss column is the post-BE stop; sim reconstructs the original from zone_mid ∓ sl_dist.",
    ],
    verdict=(
        "KEEP — calibrated for ranking (directional). Re-run first thing after the MT5 "
        "M1 candle export lands; until then no experiment quotes dollar numbers."
    ),
)
print(f"\nwrote {OUT / 'replay_vs_recorded.csv'} and RESULTS.md")
