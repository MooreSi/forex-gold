"""Reproducible audit and fixed, retrospective walk-forward challengers.

Reads the sanitized snapshot produced by capture.py. No backend imports.
Uses recorded outcomes only: no new trade replay or simulator fork.
"""
from __future__ import annotations
import ast
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import roc_auc_score
from lightgbm import LGBMRegressor

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from _shared.lib import metrics, report, splits
from prototype import causal_train, net_virtual_r, select_trades

# Fixed before observing challenger results. Retain observed market/clock inputs;
# omit account history, channel imitation and reconstructed macro observations.
MARKET = list(range(1, 13)) + [15, 18, 19, 21, 24, 25, 26, 27]
MARGIN = 0.05
SEED = 107


class MarkdownTable:
    """report.write_results table protocol, without optional tabulate install."""
    def __init__(self, frame):
        self.frame = frame

    def to_markdown(self):
        headers = ["configuration"] + list(self.frame.columns)
        lines = ["| " + " | ".join(headers) + " |",
                 "| " + " | ".join(["---"]*len(headers)) + " |"]
        for index, row in self.frame.iterrows():
            values = [str(index)] + [f"{x:.4g}" if isinstance(x, (float, np.floating)) else str(x) for x in row]
            lines.append("| " + " | ".join(values) + " |")
        return "\n".join(lines)


def vector(value):
    try:
        v = json.loads(value)
        if not isinstance(v, list) or len(v) < 24 or len(v) > 38:
            return None
        # Source: ml_engine/_feature_schema.py append-only FVG neutral values.
        v = v[:28] + [0.0, 5.0, 0.5, 0.0][max(0, len(v)-24):] if len(v) < 28 else v[:28]
        return v if len(v) == 28 and np.isfinite(np.asarray(v, float)).all() else None
    except (ValueError, TypeError):
        return None


def bootstrap_days(df, column="tpl_r", n=2000):
    groups = df.groupby("day")[column].agg(["sum", "count"])
    if len(groups) < 2:
        return [None, None]
    rng = np.random.default_rng(SEED)
    picks = rng.integers(0, len(groups), size=(n, len(groups)))
    means = groups["sum"].to_numpy()[picks].sum(1) / groups["count"].to_numpy()[picks].sum(1)
    return np.quantile(means, [0.025, 0.975]).round(4).tolist()


def main():
    manifest = json.loads((HERE / "output/manifest.json").read_text())
    path = HERE.parent / manifest["snapshot"]
    if hashlib.sha256(path.read_bytes()).hexdigest() != manifest["sha256"]:
        raise ValueError("Snapshot changed")
    c = sqlite3.connect(path.resolve().as_uri()+"?mode=ro", uri=True)
    df = pd.read_sql_query("SELECT * FROM re_signals ORDER BY created_at,id", c)
    ledger = pd.read_sql_query("SELECT * FROM re_ledger_demo ORDER BY open_time,id", c)
    status = json.loads(c.execute("SELECT value FROM edge_status").fetchone()[0])
    c.close()
    df["day"] = pd.to_datetime(df.created_at, unit="s", utc=True).dt.strftime("%Y-%m-%d")
    df["month"] = df.day.str[:7]
    df["virtual_r"] = df.apply(lambda r: net_virtual_r(r.to_dict()), axis=1)
    df["features"] = df.ml_features_json.map(vector)
    df["label_available_at"] = np.maximum(df.trigger_time.fillna(0) + 6*3600,
                                            df.close_time.fillna(0))
    closed = df[(df.status == "closed") & df.outcome.isin(["win", "loss", "be"])]
    virtual = closed[closed.virtual_r.notna()]
    labels = df[df.tpl_r.notna() & df.trigger_time.notna() & df.features.notna()].copy()
    labels["X"] = labels.features.map(lambda f: [f[i] for i in MARKET])
    # Faithful current reader repairs the historical range-regime substitution.
    for i in labels.index:
        if labels.at[i, "adx"] < 14 and labels.at[i, "features"][19] == 0.5:
            labels.at[i, "X"][MARKET.index(19)] = 0.0
    numbers = {"recorded virtual (all history)": metrics.summarize(virtual.virtual_r, len(df)),
               "old template labels (all history)": metrics.summarize(labels.tpl_r, len(df))}
    grouped = []
    for key in ["month", "direction", "level_type", "session", "live_exec_status"]:
        for value, group in labels.groupby(key, dropna=False):
            grouped.append({"group_by": key, "value": str(value), **metrics.summarize(group.tpl_r, len(group))})
    pd.DataFrame(grouped).to_csv(HERE / "output/strata.csv", index=False)
    preds, fold_notes = [], []
    days = sorted(labels.day.unique())
    # Every whole test day follows every training day; labels must have resolved.
    for raw_train, test in splits.day_folds(labels, min_train_days=max(10, int(len(days)*0.4))):
        cut = pd.Timestamp(test.day.iloc[0], tz="UTC").timestamp()
        train = causal_train(raw_train, cut, 3600)
        if len(train) < 300:
            continue
        assert train.label_available_at.max() < cut-3600
        X, y = train.X.tolist(), train.tpl_r.tolist()
        ridge = make_pipeline(StandardScaler(), Ridge(alpha=100.0)).fit(X, y)
        tree = LGBMRegressor(n_estimators=100, learning_rate=0.03, num_leaves=7,
                            min_child_samples=100, random_state=SEED, verbosity=-1,
                            n_jobs=1).fit(np.asarray(X), y)
        # Negative control: destroy the feature/label relationship in TRAIN only.
        shuffled = make_pipeline(StandardScaler(), Ridge(alpha=100.0)).fit(
            X, np.random.default_rng(SEED).permutation(y))
        p = test[["id", "created_at", "trigger_time", "day", "tpl_r", "ml_prob", "ml_prob_at_fill"]].copy()
        p["ridge"] = ridge.predict(test.X.tolist())
        p["tree"] = tree.predict(np.asarray(test.X.tolist()))
        p["shuffled"] = shuffled.predict(test.X.tolist())
        p["extra_cost"] = p.tpl_r - 0.25/5.0
        preds.append(p)
        fold_notes.append({"day": test.day.iloc[0], "training_rows": len(train),
                           "test_rows": len(test), "last_label_available_at": train.label_available_at.max(),
                           "test_start": cut})
    oos = pd.concat(preds).sort_values(["created_at", "id"])
    selections = {"same test rows unfiltered": np.ones(len(oos), dtype=bool),
                  "logged creation score >=0": oos.ml_prob.notna() & (oos.ml_prob >= 0),
                  "ridge expected R >0.05": select_trades(oos.ridge, MARGIN),
                  "shallow tree R >0.05": select_trades(oos.tree, MARGIN),
                  "shuffled-label control >0.05": select_trades(oos.shuffled, MARGIN)}
    diagnostics = {}
    curves = {}
    for name, mask in selections.items():
        selected = oos.loc[mask]
        numbers[name] = metrics.summarize(selected.tpl_r, len(oos))
        numbers[name+" (+0.25pt cost)"] = metrics.summarize(selected.extra_cost, len(oos))
        diagnostics[name] = {"day_bootstrap_95pct_mean_r": bootstrap_days(selected),
                             "days_with_trades": int(selected.day.nunique()),
                             "halves_mean_r": [float(x.tpl_r.mean()) if len(x) else None
                                               for x in [selected.iloc[:len(selected)//2],
                                                         selected.iloc[len(selected)//2:]]]}
        if name in ["same test rows unfiltered", "logged creation score >=0"]:
            label = "All test signals" if name == "same test rows unfiltered" else "Logged score >=0"
            curves[label] = selected.tpl_r.tolist()
    report.equity_chart({"Ridge (13 trades)": oos.loc[selections["ridge expected R >0.05"], "tpl_r"].tolist(),
                        "Tree (24 trades)": oos.loc[selections["shallow tree R >0.05"], "tpl_r"].tolist(),
                        "Shuffled (15 trades)": oos.loc[selections["shuffled-label control >0.05"], "tpl_r"].tolist()},
                       HERE / "output/challengers.png", "Small selected samples: no proven edge")
    for name in ["ridge", "tree", "shuffled", "ml_prob"]:
        valid = oos[name].notna()
        diagnostics[name] = {"auc_net_positive": float(roc_auc_score(
            (oos.loc[valid, "tpl_r"] > 0).astype(int), oos.loc[valid, name])),
                             "mean_predicted_r": float(oos.loc[valid, name].mean()),
                             "mean_actual_r": float(oos.loc[valid, "tpl_r"].mean())}
    oos.to_csv(HERE / "output/predictions.csv", index=False)
    pd.DataFrame(fold_notes).to_csv(HERE / "output/folds.csv", index=False)
    # Stored vectors are not necessarily full width: audit raw widths separately.
    widths = Counter(len(json.loads(v)) for v in df.ml_features_json.dropna())
    feature_quality = []
    for i, name in enumerate(manifest["features"]):
        vals = [json.loads(v)[i] for v in df.ml_features_json.dropna() if len(json.loads(v)) > i]
        vc = pd.Series(vals).value_counts()
        feature_quality.append({"feature": name, "present": len(vals), "unique": len(vc),
                                "modal_fraction": float(vc.iloc[0]/len(vals)) if len(vals) else None})
    pd.DataFrame(feature_quality).to_csv(HERE / "output/feature_quality.csv", index=False)
    weights = np.exp(-0.017*np.arange(len(virtual)))
    audit = {"signals": len(df), "closed": len(closed), "virtual_trainable": len(virtual),
             "executed": int((closed.live_exec_status == "executed").sum()),
             "tpl_labels": len(labels), "days": len(days), "widths": dict(widths),
             "decay_half_life_rows": float(np.log(2)/0.017),
             "decay_effective_rows": float(weights.sum()**2/(weights@weights)),
             "edge_status_at_capture": status, "challengers": diagnostics,
             "broker_demo_ledger_raw": {"rows": len(ledger), "pnl_dollars": float(ledger.pnl_dollars.sum()),
                                         "r_available": int(ledger.r_realised.notna().sum()),
                                         "unique_tickets": int(ledger.mt5_ticket.nunique())}}
    # A ledger blends virtual outcomes and broker outcomes: identify matched broker
    # rows by stable signal_ref, keeping latest receipt per reference within node.
    refs = set(closed.loc[closed.live_exec_status == "executed", "signal_ref"])
    broker = ledger[ledger.trade_id.isin(refs)].copy()
    audit["broker_demo_ledger_matched"] = {"rows": len(broker), "unique_refs": int(broker.trade_id.nunique()),
                                            "pnl_dollars": float(broker.pnl_dollars.sum()),
                                            "r_available": int(broker.r_realised.notna().sum())}
    audit["environment"] = {"python": sys.version.split()[0], "numpy": np.__version__, "pandas": pd.__version__}
    audit["test_window"] = [oos.day.min(), oos.day.max()]
    # Companion metric includes initial zero equity; the established shared
    # summarize() omits it and can understate drawdown on a losing first trade.
    audit["drawdown_from_initial_zero_r"] = {}
    for name, mask in selections.items():
        equity = np.r_[0, oos.loc[mask, "tpl_r"].cumsum().to_numpy()]
        audit["drawdown_from_initial_zero_r"][name] = float((equity-np.maximum.accumulate(equity)).min())
    (HERE / "output/audit.json").write_text(json.dumps(audit, indent=2, allow_nan=False))
    table = metrics.summary_table(numbers)
    table.to_csv(HERE / "output/metrics.csv")
    extra = {"Validation": f"{len(oos)} test rows on {oos.day.nunique()} days; expanding training on earlier whole days only. "
             "Actual label-availability times plus one-hour gap. Fixed models and +0.05R threshold. "
             "Retrospective research; no untouched future holdout. Confidence intervals in output/audit.json.",
             "Diagnostics": MarkdownTable(pd.DataFrame({k:v for k,v in diagnostics.items() if k in ['ridge','tree','shuffled','ml_prob']}).T).to_markdown()}
    report.write_results(HERE, "004 — Reversal edge review", "Cleaner validation does not establish a deployable gold trading edge; review the full comparison below.",
                         MarkdownTable(table), ["tpl_r replays the OLD fixed template with 0.575pt costs; it does not label today's dynamic ATR template.",
                                 "Creation-time vectors and creation-time logged scores; fill-time vectors are not stored. This cannot validate a fill-time model.",
                                 "Historical labels span multiple management/code epochs; copied history is not automatically point-in-time reproducible.",
                                 "Overlapping trades and variable trade count; cumulative per-trade curves are descriptive, not a portfolio backtest.",
                                 "All labels are recorded; no fresh M1/tick replay. Cost stress adds 0.25pt divided by the OLD 5pt stop.",
                                 "Several challengers inspected: any apparent winner needs a new locked forward period and correction for research selection.",
                                 "Shared metric table omits initial zero equity in drawdown; corrected companion values are in output/audit.json."],
                         "NEEDS-MORE-DATA — freeze the target policy and record fresh decision-time features before promotion.",
                         price_source="stored virtual outcomes and stored M1 template labels (no new replay)",
                         data_window=f"{df.day.min()} → {df.day.max()}; snapshot {manifest['captured_at_utc']}",
                         chart_series=curves, extra_sections=extra)
    print(table.to_string())
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
