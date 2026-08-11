# %% [markdown]
# # 002 — ML prob autopsy: why does the engine's model score losers HIGHER?
#
# **Finding that triggered this**: on the snapshot, mean ml_prob is 0.154 for
# losers vs 0.078 for winners — the model ranks bad signals above good ones.
#
# **Hypotheses to test**
#   H1: ml_prob is genuinely anti-predictive (AUC < 0.5) — not just a mean shift.
#   H2: gating signals on ml_prob (either direction) changes expectancy.
#   H3: a fresh model retrained walk-forward on the stored 24 features beats it.
#
# **Data**: re_signals (741 rows, all with 24-float feature vectors and, for
# 607 win/loss rows, an outcome label). Labels come from the RECORDED book.

# %% setup
import sys
from pathlib import Path

import numpy as np
import pandas as pd

LAB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(LAB / "_shared"))

from lib import loaders, metrics, splits

OUT = Path(__file__).resolve().parent / "output"
OUT.mkdir(exist_ok=True)

sig = loaders.signals_df()
settled = sig[sig["outcome"].isin(["win", "loss"])].copy()
settled["y"] = (settled["outcome"] == "win").astype(int)
settled["r_rec"] = settled["net_pnl_dollars"] / 50.0  # sl_risk_usd ~= $50
print(f"{len(settled)} settled signals, base win rate {settled['y'].mean():.1%}, "
      f"total recorded {settled['r_rec'].sum():.1f}R")

# %% H1 — is ml_prob anti-predictive as a *ranker*?
have = settled.dropna(subset=["ml_prob"])
a = metrics.auc(have["y"], have["ml_prob"])
print(f"AUC of stored ml_prob for predicting WIN: {a:.3f}  (0.5 = coin flip; <0.5 = inverted)")
# also vs R-multiple (the model claims to predict R): rank corr
rc = have["ml_prob"].corr(have["r_rec"], method="spearman")
print(f"Spearman(ml_prob, recorded R): {rc:.3f}")

# %% H2 — threshold sweep, walk-forward honest (threshold fixed per fold on train)
def sweep(df, col, thresholds, direction=">"):
    rows = []
    for t in thresholds:
        kept = df[df[col] > t] if direction == ">" else df[df[col] < t]
        m = metrics.summarize(kept["r_rec"], n_candidates=len(df))
        m["threshold"] = f"{col}{direction}{t}"
        rows.append(m)
    base = metrics.summarize(df["r_rec"], n_candidates=len(df))
    base["threshold"] = "no filter"
    rows.insert(0, base)
    return pd.DataFrame(rows).set_index("threshold")

print("\nIn-sample sweep (context only, NOT a result):")
tab = sweep(have, "ml_prob", [-0.1, 0.0, 0.05, 0.1, 0.2], ">")
tab2 = sweep(have, "ml_prob", [0.2, 0.1, 0.05, 0.0], "<")
print(pd.concat([tab, tab2])[["trades", "total_R", "expectancy_R", "win_rate", "profit_factor"]])

# %% H2 walk-forward: pick best threshold on train days, apply to test day
wf_rows = []
for train, test in splits.day_folds(have, min_train_days=3):
    best_t, best_e = None, -np.inf
    for t in np.arange(-0.2, 0.31, 0.05):
        for d in (">", "<"):
            kept = train[train["ml_prob"] > t] if d == ">" else train[train["ml_prob"] < t]
            if len(kept) < 10:
                continue
            e = kept["r_rec"].mean()
            if e > best_e:
                best_e, best_t = e, (t, d)
    if best_t is None:
        continue
    t, d = best_t
    kept = test[test["ml_prob"] > t] if d == ">" else test[test["ml_prob"] < t]
    wf_rows.append({"day": test["day"].iloc[0], "rule": f"ml_prob{d}{t:.2f}",
                    "test_trades": len(kept), "test_R": kept["r_rec"].sum(),
                    "unfiltered_R": test["r_rec"].sum()})
wf = pd.DataFrame(wf_rows)
print("\nWalk-forward ml_prob gating (rule chosen on prior days only):")
print(wf.to_string(index=False))
print(f"\nTotals: gated {wf['test_R'].sum():.1f}R vs unfiltered {wf['unfiltered_R'].sum():.1f}R")

# %% H3 — retrain fresh: logistic regression on the stored 24 features, walk-forward
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import make_pipeline

X_all = np.vstack(settled["features"].to_numpy())
settled = settled.reset_index(drop=True)
aucs, day_rows = [], []
for train, test in splits.day_folds(settled, min_train_days=3):
    Xtr = np.vstack(train["features"].to_numpy()); ytr = train["y"].to_numpy()
    Xte = np.vstack(test["features"].to_numpy());  yte = test["y"].to_numpy()
    if len(np.unique(ytr)) < 2 or len(test) < 5:
        continue
    clf = make_pipeline(StandardScaler(), LogisticRegression(max_iter=1000, C=0.5))
    clf.fit(Xtr, ytr)
    p = clf.predict_proba(Xte)[:, 1]
    aucs.append(metrics.auc(pd.Series(yte), pd.Series(p)))
    # trade only the top half by predicted prob, measured on recorded R
    cut = np.median(p)
    day_rows.append({"day": test["day"].iloc[0], "auc": round(aucs[-1], 3),
                     "top_half_R": test.loc[p >= cut, "r_rec"].sum(),
                     "bottom_half_R": test.loc[p < cut, "r_rec"].sum(),
                     "all_R": test["r_rec"].sum()})
fresh = pd.DataFrame(day_rows)
print("\nFresh logistic model, walk-forward by day:")
print(fresh.to_string(index=False))
print(f"\nMean walk-forward AUC: {np.mean(aucs):.3f}"
      f" | top-half total {fresh['top_half_R'].sum():.1f}R"
      f" vs bottom-half {fresh['bottom_half_R'].sum():.1f}R"
      f" vs all {fresh['all_R'].sum():.1f}R")

# %% persist
tab.to_csv(OUT / "insample_sweep.csv")
wf.to_csv(OUT / "walkforward_gating.csv", index=False)
fresh.to_csv(OUT / "fresh_model_walkforward.csv", index=False)
print(f"\nwrote 3 csvs to {OUT}")
print("RESULT: fill in README.md from the numbers above.")
