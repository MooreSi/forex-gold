"""Run isolated, exact source fragments to demonstrate defects without imports.

No application module initialization, database calls, or model files loaded.
"""
from __future__ import annotations
import __future__
import ast
from datetime import datetime, timezone
import json
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
BASE = HERE.parents[1] / "backend/src/services/reversal_engine"


def fragment(filename, functions, namespace):
    # Freeze the audit's source version: subsequent correctness fixes must
    # not invalidate the historical demonstration of what was reviewed.
    manifest = json.loads((HERE / "output/manifest.json").read_text())
    path = (BASE / filename).resolve().relative_to(HERE.parents[1])
    source = subprocess.check_output(["git", "show", f"{manifest['git_head']}:{path.as_posix()}"],
                                     cwd=HERE.parents[1], text=True)
    tree = ast.parse(source)
    body = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in functions]
    if len(body) != len(functions):
        raise ValueError("Missing requested functions")
    module = ast.Module(body=body, type_ignores=[])
    exec(compile(module, filename, "exec", flags=__future__.annotations.compiler_flag), namespace)
    return namespace


def main():
    ns = fragment("level_detector.py", ["_utc_hour", "get_asia_range"],
                  {"datetime": datetime, "timezone": timezone})
    candles = [{"time": datetime(2026, 10, day, hour, tzinfo=timezone.utc).timestamp(),
                "low": low, "high": high}
               for day, low, high in [(5, 100, 110), (6, 200, 210)]
               for hour in range(8)]
    asia = ns["get_asia_range"](candles)
    assert asia == (100.0, 210.0) and asia != (200, 210)
    ns = fragment("pro_model.py", ["_vector"], {})
    fvg = ns["_vector"]("BUY", 50, 20, 8, 0, {"fvg_dist_norm": 0.0})[5]
    assert fvg == 5.0
    ns = fragment("macro_backfill.py", ["_closes_at"], {})
    # The lookup selects an hourly close stamped at 10:00 during that hour.
    # This is a conditional leakage reproduction: verify vendor timestamp
    # semantics before promoting it to a confirmed end-to-end data finding.
    macro = ns["_closes_at"]([(10*3600, 100), (11*3600, 200)], 11*3600+600)
    assert macro == (200, 100)
    ns = fragment("../market/validation.py", ["purged_kfold"], {})
    folds = ns["purged_kfold"](8, 4, [(i*10, i*10+1) for i in range(8)])
    assert max(folds[0][0]) > max(folds[0][1])
    result = {"asia_latest_complete_expected": [200, 210], "asia_actual_multi_day": asia,
              "pro_fvg_distance_zero_expected": 0, "pro_fvg_distance_actual": fvg,
              "purged_kfold_first_train_indices": folds[0][0],
              "purged_kfold_first_test_indices": folds[0][1],
              "macro_conditional_hourly_close_lookup": macro,
              "macro_caveat": "If vendor stamps hourly bars at OPEN, close must not be available until +3600s; timestamp semantics not revalidated against vendor here."}
    (HERE / "output/defect_reproductions.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
