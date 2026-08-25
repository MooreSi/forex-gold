#!/usr/bin/env python3
"""Standalone live capture of reference-channel signal snapshots.

The same capture the app now runs internally (core_signal_snapshot), but as
its own process so data collection can start WITHOUT restarting the app --
the in-app loop only begins at the next restart, and signals arriving in
the meantime would otherwise be lost forever. Both write the same table and
both skip anything already captured, so running both at once is safe and
simply means whichever notices a signal first records it.

Forward-only by design: it will not reach back and snapshot old signals
against today's candles, because a reconstructed reading is not evidence of
what the market looked like when they actually fired.

Usage:
    tools/tg_signal_snapshot_watch.py                 # run until stopped
    tools/tg_signal_snapshot_watch.py --once          # single sweep
    tools/tg_signal_snapshot_watch.py --report        # what has been collected
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time

sys.path.insert(0, "/Users/simon/Documents/FOREX.nosync")

DB = "/Users/simon/Library/Application Support/ForexTrader/data/forex_trader_demo.db"
POLL_S = 5
_last_bg = 0.0


def _report() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT group_name, stage, direction, session, "
            "       datetime(captured_at,'unixepoch') t, capture_lag_s, "
            "       price, entry_low, entry_high, indicators_json, fvg_json "
            "FROM tg_signal_snapshots ORDER BY captured_at DESC LIMIT 20").fetchall()]
        total = con.execute("SELECT COUNT(*) FROM tg_signal_snapshots").fetchone()[0]
    except sqlite3.OperationalError as e:
        print(f"table not created yet ({e}) -- start the app or run without --report first")
        return 1
    finally:
        con.close()

    print(f"tg_signal_snapshots rows: {total}\n")
    if not rows:
        print("Nothing captured yet. Rows appear as signals arrive.")
        return 0
    for r in rows:
        ind = json.loads(r["indicators_json"] or "{}")
        m15 = ind.get("M15", {})
        fvg = json.loads(r["fvg_json"] or "{}")
        zone = (f"{r['entry_low']}-{r['entry_high']}" if r["entry_low"] else "(none)")
        print(f"{r['t']}  {r['group_name'][:24]:24} {r['stage']:11} {str(r['direction'] or ''):4} "
              f"px {r['price']:.2f}  zone {zone:>17}  sess {r['session']:7} "
              f"lag {r['capture_lag_s']}s")
        if m15:
            print(f"    M15  rsi {m15.get('rsi14')}  atr {m15.get('atr14')}  "
                  f"adx {m15.get('adx14')}  ema {m15.get('ema_stack')}  "
                  f"vol x{m15.get('volume_ratio')}  |  FVG conf {fvg.get('fvg_confluence')} "
                  f"fresh {fvg.get('fvg_fresh')} open-gaps {fvg.get('n_open_gaps')}")
    return 0


async def _run(once: bool) -> int:
    from backend.src.db import database as db
    db.init(DB)
    from backend.src.services.positions.core_signal_snapshot import (
        capture_pending_snapshots, capture_background_snapshot, WATCHED)
    from backend.src.services.broker.mt5_client import MT5BridgeClient

    bridge = MT5BridgeClient("http://localhost:9010")

    tick = await bridge.get_tick()
    if tick is None:
        print("MT5 bridge not reachable on localhost:9010 -- is the app running?")
        return 1

    print(f"watching {', '.join(WATCHED)} (poll {POLL_S}s). Ctrl-C to stop.", flush=True)
    while True:
        try:
            n = await capture_pending_snapshots(bridge)
            if n:
                print(f"{time.strftime('%H:%M:%S')}  captured {n} snapshot(s)", flush=True)
            # background negatives on the same 15-min cadence as the app
            global _last_bg
            if time.time() - _last_bg > 900:
                _last_bg = time.time()
                b = await capture_background_snapshot(bridge)
                if b:
                    print(f"{time.strftime('%H:%M:%S')}  background x{b}", flush=True)
        except Exception as e:
            print(f"{time.strftime('%H:%M:%S')}  sweep error: {e}", flush=True)
        if once:
            return 0
        await asyncio.sleep(POLL_S)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    if a.report:
        return _report()
    try:
        return asyncio.run(_run(a.once))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
