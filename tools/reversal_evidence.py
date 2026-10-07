"""Inspect/evaluate the reversal research store. Never places or changes orders.

Run with .venv/bin/python -m tools.reversal_evidence --help.
"""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import os
import time
import httpx
from backend.src import config
from backend.src.services.reversal_engine.evidence import experiments, paid_feeds
from backend.src.services.reversal_engine.evidence.evidence_repo import EvidenceStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "evaluate", "import-cme"])
    parser.add_argument("--data-dir", type=Path, default=config.DATA_DIR)
    parser.add_argument("--env", choices=["demo", "live"], default="demo")
    parser.add_argument("--symbol", help="explicit COMEX contract, e.g. GCZ6; no automatic rolls")
    parser.add_argument("--start", help="UTC ISO timestamp, historical import")
    parser.add_argument("--end", help="UTC ISO timestamp, historical import")
    args = parser.parse_args()
    path = args.data_dir / "reversal_evidence.db"
    if args.action == "status" and not path.exists():
        print(json.dumps({"state": "not_started", "detail": "collector starts with the updated app research loop"}))
        return
    store = EvidenceStore(path)
    if args.action == "evaluate":
        print(json.dumps(experiments.run_candidates(store, args.env, args.data_dir / "reversal_experiments"), indent=2))
    elif args.action == "import-cme":
        if not all((args.symbol, args.start, args.end, os.environ.get("DATABENTO_API_KEY"))):
            parser.error("import-cme needs --symbol, --start, --end and DATABENTO_API_KEY; licensed data may incur cost")
        async def perform():
            async def emit(event):
                await asyncio.to_thread(store.put, args.env, event)
            async with httpx.AsyncClient(timeout=30) as client:
                await paid_feeds.historical_books(client, os.environ["DATABENTO_API_KEY"],
                                                  args.symbol, args.start, args.end, emit)
        asyncio.run(perform())
    else:
        status = store.status()
        for value in status.values():
            value["age_s"] = round(time.time() - value["ts"], 1)
        runs = [{"id": r["id"], "exported": bool(r["exported"]), "env": r["payload"]["env"],
                 "metrics": r["payload"]["metrics"]} for r in store.runs()]
        print(json.dumps({"providers": status, "labels": len(store.labels(args.env)),
                          "experiments": runs, "promotion": "manual review required"}, indent=2))


if __name__ == "__main__":
    main()
