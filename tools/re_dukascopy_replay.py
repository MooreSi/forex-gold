#!/usr/bin/env python3
"""Replay the Reversal Engine's level-touch entries on years of Dukascopy gold.

docs/todo/reversal-engine/260. Two sub-commands:

    download   fetch Dukascopy XAUUSD tick hours into a local cache (resumable)
    replay     build M1 bars, regenerate signals with the live level_detector,
               and judge them against placebo entries with the 240 study

RESEARCH ONLY. It reads no database, talks to no broker and places nothing.
The only network access is `download`, to datafeed.dukascopy.com. The cache
is under the app data directory and is never committed; Dukascopy's terms
could not be retrieved on 2026-10-01, so redistribution is not assumed.

Usage:
    .venv/bin/python -m tools.re_dukascopy_replay download --start 2024-01-01 --end 2024-01-15
    .venv/bin/python -m tools.re_dukascopy_replay replay   --start 2024-01-01 --end 2024-01-15 \
                                                           [--every 3] [--cost 0.575] [--out rows.json]
"""
from __future__ import annotations

import argparse
import http.client
import json
import random
import ssl
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.src.config import DATA_DIR
from backend.src.services.reversal_engine import entry_study as es
from backend.src.services.reversal_engine import historical_bars as hb
from backend.src.services.reversal_engine import historical_signals as hs
from backend.src.services.reversal_engine import replay_verdict as rv
from tools import re_entry_study as study

DEFAULT_CACHE = DATA_DIR / "research_dukascopy"
SYMBOL = "XAUUSD"
_UA = "Mozilla/5.0 (research; forex-gold replay tool)"
# Signals need 50 H1 candles and the study 26h of bars before the first one.
_WARMUP_DAYS = 3

LEFT_OUT = (
    "Not modelled: ML and meta-label gates, news and spread gates, the Claude review, "
    "the consecutive-loss cooldown, the six-open-signal cap, liquidity-map levels, "
    "cross-engine conflicts, session toggles. This is the level-touch family, not a "
    "replay of what the engine traded."
)


def _day(s: str) -> float:
    return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


# Same precedent as utils/news_calendar.py: trust certifi's bundle so HTTPS works
# off a stock python.org build. Verification stays on either way.
try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _SSL_CTX = ssl.create_default_context()


_HOST = "datafeed.dukascopy.com"
_local = threading.local()


def fetch(url: str):
    """Body, None for 404 (no such hour), RateLimited for 429, raise otherwise.

    One persistent TLS connection per thread: measured 2026-10-01, the
    handshake to this host took 7 s against 0.5 s for the request itself, so
    a connection per hour made a year of data a day-long download.
    """
    path = url.split(_HOST, 1)[1]
    for fresh in (False, True):
        conn = getattr(_local, "conn", None)
        if conn is None or fresh:
            if conn is not None:
                conn.close()
            conn = http.client.HTTPSConnection(_HOST, timeout=60, context=_SSL_CTX)
            _local.conn = conn
        try:
            conn.request("GET", path, headers={"User-Agent": _UA})
            resp = conn.getresponse()
            body = resp.read()
        except (http.client.HTTPException, OSError):
            _local.conn = None
            if fresh:
                raise
            continue
        if resp.status == 404:
            return None
        if resp.status == 429:
            _local.conn = None
            raise hb.RateLimited(float(resp.getheader("Retry-After") or 120))
        if resp.status != 200:
            raise OSError(f"HTTP {resp.status} for {url}")
        return body


def cmd_download(a) -> int:
    start, end = _day(a.start), min(_day(a.end), time.time() - 3 * 3600)
    cache = Path(a.cache)
    hours = int((end - start) // 3600)
    print(f"{a.symbol} {a.start} to {a.end}: up to {hours} hours into {cache} "
          f"({a.workers} worker(s), {a.pause}s pause after each request)", flush=True)
    t0 = time.time()
    r = hb.download_range(fetch, cache, a.symbol, start, end, workers=a.workers,
                          pause_s=a.pause)
    print(f"fetched {r.fetched}, already cached {r.cached}, no data {r.empty}, "
          f"skipped (market shut) {r.skipped_closed}, failed {len(r.failed)} "
          f"in {time.time() - t0:.0f}s")
    if r.rate_limited:
        print(f"STOPPED: the server kept answering 429 (rate limited). {r.deferred} hours "
              f"were left for next time; run the same command later and it resumes.")
    for hour, msg in r.failed[:10]:
        print(f"  FAILED {datetime.fromtimestamp(hour, timezone.utc):%Y-%m-%d %H}h: {msg}")
    return 1 if (r.failed or r.rate_limited) else 0


def _fmt(name: str, s) -> str:
    if not s or s.get("win_rate") is None:
        return f"  {name:34s} (no rows)"
    return (f"  {name:34s} n={s['n']:6d} cl={s['clusters']:5d} win={s['win_rate'] * 100:5.1f}% "
            f"placebo={s['placebo'] * 100:5.1f}% z={s['z']:+6.2f}  R={s['mean_r']:+.3f}"
            f"{'  <-- z>=4' if rv.slice_is_signal(s) else ''}")


def report(rows: list[dict]) -> None:
    ts = sorted(r["ts"] for r in rows)
    print(f"\nSignals replayed: {len(rows)} "
          f"({datetime.fromtimestamp(ts[0], timezone.utc):%Y-%m-%d} to "
          f"{datetime.fromtimestamp(ts[-1], timezone.utc):%Y-%m-%d})")
    print(LEFT_OUT)
    tpl = [r["tpl_r"] for r in rows if r.get("tpl_r") is not None]
    if tpl:
        print(f"\nTemplate replay (the live exits) mean R after costs: "
              f"{sum(tpl) / len(tpl):+.3f} over {len(tpl)}")

    print("\n1. The bar (z>=3, n>=200, R>0 in both halves, R>0 in >2/3 of years)")
    for key, label in (("b55", "touch entry, stop 5 / target 5"),
                       ("b54", "touch entry, stop 5 / target 4")):
        j = rv.judge(rows, key)
        print(_fmt(label, j.summary))
        print(f"    verdict: {'PASSES' if j.passes else 'no edge'}"
              + ("" if j.passes else "  (" + "; ".join(j.failures) + ")"))
        for y, s in j.years.items():
            tag = "" if y in j.counted_years else "  (under 200, not counted)"
            print("  " + _fmt(f"  {y}", s) + tag)

    print("\n2. Slices, stop 5 / target 5 (a slice needs z>=4: about 30 are shown)")
    for g in ("level_type", "session", "direction"):
        for v in sorted({str(r[g]) for r in rows}):
            sub = [r for r in rows if str(r[g]) == v]
            if len(sub) >= 200:
                print(_fmt(f"{g}={v}", rv.summarise(sub, "b55")))

    print("\n3. Facts about the approach, by quintile, stop 5 / target 5")
    for f in ("approach_5", "approach_15", "approach_60", "range_ratio_3", "volume_ratio_5",
              "touches_24h", "mins_since_touch", "range_4h", "stretch_60", "hour"):
        vals = [r[f] for r in rows if r.get(f) is not None]
        edges = es.quantile_edges(vals)
        for q in range(len(edges) + 1):
            sub = [r for r in rows if r.get(f) is not None and es.bucket(r[f], edges) == q]
            print(_fmt(f"{f} q{q + 1}", rv.summarise(sub, "b55")))


def cmd_replay(a) -> int:
    start, end = _day(a.start), min(_day(a.end), time.time())
    cache = Path(a.cache)
    t0 = time.time()
    bars = hb.load_m1(cache, a.symbol, start - _WARMUP_DAYS * 86400, end + 86400)
    if not bars:
        print("no bars in the cache for that range; run `download` first")
        return 2
    print(f"{len(bars)} M1 bars loaded in {time.time() - t0:.0f}s "
          f"({datetime.fromtimestamp(bars[0].ts, timezone.utc):%Y-%m-%d} to "
          f"{datetime.fromtimestamp(bars[-1].ts, timezone.utc):%Y-%m-%d})", flush=True)
    t0 = time.time()
    gens = [g for g in hs.regenerate(bars, a.cycle_min * 60) if start <= g.sig.created_at < end]
    print(f"{len(gens)} signals regenerated in {time.time() - t0:.0f}s "
          f"(cycle {a.cycle_min} min)", flush=True)
    gens = gens[:: max(1, a.every)]
    if a.every > 1:
        print(f"every {a.every}th kept: {len(gens)} to replay", flush=True)

    ts_index = [b.ts for b in bars]
    rng = random.Random(a.seed)
    rows, t0 = [], time.time()
    for i, g in enumerate(gens, 1):
        s = g.sig
        row = study.study_signal(
            {"id": s.id, "created_at": s.created_at, "direction": s.direction,
             "entry_low": s.entry_low, "entry_high": s.entry_high,
             "level_price": s.level_price, "level_type": s.level_type,
             "session": s.session, "live_exec_status": None, "mt5_ticket": None,
             "ml_prob": None}, bars, ts_index, a.cost, rng)
        if row is not None:
            rows.append(row)
        if i % 500 == 0:
            print(f"  {i}/{len(gens)} replayed, {time.time() - t0:.0f}s", flush=True)
    if not rows:
        print("no signal reached its zone")
        return 2
    if a.out:
        Path(a.out).write_text(json.dumps(rows))
        print(f"rows written to {a.out}")
    report(rows)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("download", "replay"):
        p = sub.add_parser(name)
        p.add_argument("--start", required=True, help="YYYY-MM-DD, inclusive")
        p.add_argument("--end", required=True, help="YYYY-MM-DD, exclusive")
        p.add_argument("--cache", default=str(DEFAULT_CACHE))
        p.add_argument("--symbol", default=SYMBOL)
        if name == "download":
            p.add_argument("--workers", type=int, default=1)
            p.add_argument("--pause", type=float, default=1.0,
                           help="seconds each worker waits after a request")
        else:
            p.add_argument("--cost", type=float, default=study.DEFAULT_COST)
            p.add_argument("--cycle-min", type=int, default=5)
            p.add_argument("--every", type=int, default=1,
                           help="keep every Nth regenerated signal (speed)")
            p.add_argument("--out", default="")
            p.add_argument("--seed", type=int, default=260)
    a = ap.parse_args()
    return cmd_download(a) if a.cmd == "download" else cmd_replay(a)


if __name__ == "__main__":
    sys.exit(main())
