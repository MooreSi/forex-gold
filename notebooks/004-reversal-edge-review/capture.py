"""Capture whitelisted research tables from read-only local SQLite connections.

No app imports. Does not inspect credential tables or copy entire databases.
Existing snapshots are never overwritten. Invoke with --data-dir PATH.
"""
from __future__ import annotations
import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def names_from_source():
    base = ROOT / "backend/src/services/reversal_engine"
    def strings(path, variable):
        tree = ast.parse(path.read_text())
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == variable for t in node.targets):
                return [x.value for x in node.value.elts if isinstance(x, ast.Constant)]
        raise ValueError(variable)
    return strings(base / "ml_engine/_feature_schema.py", "FEATURE_NAMES") + strings(
        base / "re_macro.py", "MACRO_FEATURE_NAMES")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
    folder = HERE.parent / "_shared/data"
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / f"reversal_research_{stamp}.db"
    if dest.exists():
        raise FileExistsError(dest)
    manifest = {"captured_at_utc": stamp, "features": names_from_source(),
                "git_head": subprocess.check_output(["git", "rev-parse", "HEAD"],
                                                     cwd=ROOT, text=True).strip(),
                "snapshot": str(dest.relative_to(HERE.parent)), "tables": {}}
    with sqlite3.connect(dest) as out:
        for filename, queries in {
            "reversal_engine.db": {
                "re_signals": "SELECT * FROM re_signals",
                "re_analysis_log": "SELECT ts,session,htf_bias,price,atr,adx,result,reason FROM re_analysis_log",
                "re_balance_log": "SELECT * FROM re_balance_log",
                "re_shadow_decisions": "SELECT * FROM re_shadow_decisions",
                "re_xasset_fits": "SELECT * FROM re_xasset_fits",
                "edge_status": "SELECT key,value FROM re_config WHERE key='edge_model_status'",
            },
            "forex_trader_demo.db": {
                "re_ledger_demo": "SELECT * FROM consolidated_trades WHERE engine='reversal_engine'",
                "re_templates": "SELECT * FROM ea_trade_templates WHERE name LIKE '%Reversal%' OR name='30 TP1 SL50 and Trail'",
            },
        }.items():
            source = sqlite3.connect((args.data_dir / filename).resolve().as_uri()+"?mode=ro", uri=True)
            source.execute("BEGIN")
            for table, query in queries.items():
                cur = source.execute(query)
                columns = [d[0] for d in cur.description]
                out.execute(f'CREATE TABLE "{table}" (' + ','.join(f'"{c}"' for c in columns) + ')')
                rows = cur.fetchall()
                out.executemany(f'INSERT INTO "{table}" VALUES (' + ','.join('?' for _ in columns) + ')', rows)
                manifest["tables"][table] = len(rows)
            if filename == "forex_trader_demo.db":
                cols = [r[1] for r in source.execute("PRAGMA table_info(vantage_risk_settings)")]
                keys = [c for c in cols if c.startswith("re_") or c.startswith("meta_label")]
                manifest["settings_demo"] = dict(zip(keys, source.execute(
                    "SELECT "+','.join(keys)+" FROM vantage_risk_settings LIMIT 1").fetchone()))
            source.close()
    manifest["sha256"] = hashlib.sha256(dest.read_bytes()).hexdigest()
    (HERE / "output").mkdir(exist_ok=True)
    (HERE / "output/manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
