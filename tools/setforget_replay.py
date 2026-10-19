"""Replay the production Set & Forget scanner on saved broker OHLC, offline.

Usage: python -m tools.setforget_replay --data-dir /path/to/bars
    --start 2026-07-01 --end 2026-10-19 --output /tmp/result.json

The directory contains D1.json, H4.json and M30.json lists. Timestamps must
share the broker's clock. Partial H4/D1 bars are rebuilt from M30, never read
from future completed OHLC. No runtime, account, AI or order API is used.
"""
from __future__ import annotations

import argparse
from bisect import bisect_right
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path

from backend.src.services.setforget import analysis, resample, setup


def _day(value: str) -> float:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def asof(bars: list[dict], stamps: list[float], t: float, period: int,
         recent: list[dict], count: int) -> list[dict]:
    """Completed history plus the partial bar visible at this M30 close."""
    idx = bisect_right(stamps, t - 1) - 1
    if idx < 0:
        return []
    latest = bars[idx]
    if latest["ts"] + period <= t:
        history = bars[max(0, idx-count+1):idx+1]
        if latest["ts"] + period == t:
            price = latest["close"]
            return (history + [{"ts": t, "open": price, "high": price,
                                "low": price, "close": price}])[-count:]
        return history
    fragments = [c for c in recent if latest["ts"] <= c["ts"] < t]
    if not fragments:
        return bars[max(0, idx-count+1):idx]
    partial = {"ts": latest["ts"], "open": fragments[0]["open"],
               "high": max(c["high"] for c in fragments),
               "low": min(c["low"] for c in fragments),
               "close": fragments[-1]["close"]}
    return bars[max(0, idx-count+1):idx] + [partial]


def replay(data: dict[str, list[dict]], start: float, end: float) -> dict:
    stamps = {tf: [c["ts"] for c in bars] for tf, bars in data.items()}
    counts, refusals = Counter(), Counter()
    days, examples, returns = set(), [], []
    position = None
    last_signal = None
    for i, bar in enumerate(data["M30"]):
        t = bar["ts"] + 1800
        instant = t - resample.MT5_UTC_OFFSET_SECONDS
        if instant < start or instant >= end:
            continue
        if position:
            buy = position["direction"] == "BUY"
            sl, tp = position["stop_loss"], position["take_profit"]
            stopped = bar["low"] <= sl if buy else bar["high"] >= sl
            won = bar["high"] >= tp if buy else bar["low"] <= tp
            if stopped or won:
                # Stop first on an ambiguous bar. Costs in gold price points.
                returns.append((-1 if stopped else position["rr"])
                               - 0.30 / position["risk"])
                position = None
        recent = data["M30"][max(0, i-60):i+1]
        daily = asof(data["D1"], stamps["D1"], t, 86400, recent, analysis.DAILY_COUNT)
        h4 = asof(data["H4"], stamps["H4"], t, 14400, recent, analysis.ENTRY_COUNT)
        ev = analysis.evidence_from(daily, h4, data["M30"][max(0,i-199):i+1])
        best, why, _ = analysis.scan(ev)
        counts[best["stage"] if best else "no_candidate"] += 1
        if best is None:
            refusals[why] += 1
            continue
        broken = setup.invalidations(best)
        if broken:
            refusals["ratio" if best.get("rr", 0) < setup.MIN_RR else best["stage"]] += 1
            continue
        counts["placeable_closes"] += 1
        days.add(datetime.fromtimestamp(instant, timezone.utc).strftime("%Y-%m-%d"))
        signal = (best["direction"], (best.get("trigger") or {}).get("ts"))
        if position is None and signal != last_signal:
            position, last_signal = best, signal
            counts["entries"] += 1
            if len(examples) < 8:
                examples.append({"at": datetime.fromtimestamp(instant, timezone.utc).isoformat(),
                                 **best})
    return {"counts": dict(counts), "days_with_setup": len(days),
            "closed_trades": len(returns), "wins": sum(r > 0 for r in returns),
            "net_r": round(sum(returns), 4),
            "mean_r": round(sum(returns)/len(returns), 4) if returns else None,
            "open_at_end": position is not None,
            "top_blockers": refusals.most_common(8), "examples": examples,
            "limitations": "No AI; one position at a time; M30 stop-first fills; "
                "$0.30 fixed price cost; no slippage/gap or financing model; "
                "counts describe this implementation, not verified strategy profitability."}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--data-dir", type=Path, required=True)
    p.add_argument("--start", required=True)
    p.add_argument("--end", required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    data = {tf: sorted(json.loads((args.data_dir/f"{tf}.json").read_text()),
                       key=lambda c: c["ts"]) for tf in ("D1", "H4", "M30")}
    result = replay(data, _day(args.start), _day(args.end))
    args.output.write_text(json.dumps(result, indent=2))
    print(json.dumps({k:v for k,v in result.items() if k != "examples"}, indent=2))


if __name__ == "__main__":
    main()
