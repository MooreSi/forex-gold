#!/usr/bin/env python3
"""Rebuild the Reversal Engine's labels and re-run its evaluation, then judge
the challengers against the bar set in advance.

docs/todo/reversal-engine/250, steps 3 and 4. The labelling arithmetic lives
in `backend/src/services/reversal_engine/own_template_label.py` and the
verdict in `edge_model.prove` / `EdgeModel.fit`, both tested there; this file
fetches, joins and prints.

READ-ONLY. It reads `reversal_engine.db`, `trend_pa.db` and the demo database
through `mode=ro` URIs and fetches M1 candles from the bridge's
`/candles_range` (cached per finished day, shared with re_entry_study).
Nothing here places, modifies or closes an order, and nothing refits a model
the app uses: `EdgeModel` instances here are local to this process.

Usage:
    .venv/bin/python -m tools.re_measurement_audit [--since 2026-07-20]
                                                   [--out report.md]
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from backend.src.config import DATA_DIR
from backend.src.services.reversal_engine import edge_model as em
from backend.src.services.reversal_engine import own_template_label as otl
from backend.src.services.reversal_engine.ml_engine._feature_schema import pad_to_schema
from backend.src.services.reversal_engine.ml_engine._training_data import _realised_r
from tools.re_entry_study import DEFAULT_COST, load_bars

H1_S = 3600
ADX_N = 14
RANGING_BELOW = 20.0
TRENDING_FROM = 25.0


def _ro(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


# ── Inputs ───────────────────────────────────────────────────────────────────

def load_signals(since: float) -> list[dict]:
    with _ro(DATA_DIR / "reversal_engine.db") as c:
        return [dict(r) for r in c.execute(
            "SELECT id, created_at, direction, strategy, trigger_price, trigger_time,"
            " status, outcome, live_exec_status, mt5_ticket, sl_dist,"
            " net_pnl_dollars, tpl_r, ml_features_json FROM re_signals"
            " WHERE created_at >= ? AND trigger_time IS NOT NULL AND trigger_price > 0"
            " ORDER BY trigger_time", (since,))]


def load_templates() -> dict:
    with _ro(DATA_DIR / "forex_trader_demo.db") as c:
        return {r["name"]: dict(r) for r in c.execute("SELECT * FROM ea_trade_templates")}


def load_broker() -> dict:
    """mt5_ticket -> (profit, dollars the initial stop put at risk)."""
    out = {}
    with _ro(DATA_DIR / "forex_trader_demo.db") as c:
        for r in c.execute(
                "SELECT mt5_ticket, mt5_profit, initial_risk FROM vantage_simulated_trades"
                " WHERE status='closed' AND mt5_ticket IS NOT NULL"
                " AND mt5_profit IS NOT NULL AND initial_risk > 0"):
            out[int(r["mt5_ticket"])] = (float(r["mt5_profit"]), float(r["initial_risk"]))
    return out


# ── Regime: Wilder ADX(14) on H1 resampled from M1 ──────────────────────────

def h1_from_m1(bars) -> list[dict]:
    """UTC-hour bars from M1, oldest first; `ts` is the bar's OPEN."""
    out: dict = {}
    for b in bars:
        k = int(b.ts // H1_S) * H1_S
        h = out.get(k)
        if h is None:
            out[k] = {"ts": k, "high": b.high, "low": b.low, "close": b.close}
        else:
            h["high"] = max(h["high"], b.high)
            h["low"] = min(h["low"], b.low)
            h["close"] = b.close
    return [out[k] for k in sorted(out)]


def wilder_adx(h1: list[dict], n: int = ADX_N) -> dict:
    """{bar close time: ADX} for every H1 bar with enough history.

    Wilder's smoothing throughout. A gap in the hours (weekend, missing
    data) is walked straight across, as MT5's own indicator does.
    """
    out: dict = {}
    if len(h1) < 2 * n + 1:
        return out
    tr_s = pdm_s = mdm_s = 0.0
    dx_hist: list[float] = []
    adx = None
    for i in range(1, len(h1)):
        cur, prev = h1[i], h1[i - 1]
        up = cur["high"] - prev["high"]
        dn = prev["low"] - cur["low"]
        pdm = up if (up > dn and up > 0) else 0.0
        mdm = dn if (dn > up and dn > 0) else 0.0
        tr = max(cur["high"] - cur["low"], abs(cur["high"] - prev["close"]),
                 abs(cur["low"] - prev["close"]))
        if i <= n:
            tr_s += tr
            pdm_s += pdm
            mdm_s += mdm
            if i < n:
                continue
        else:
            tr_s = tr_s - tr_s / n + tr
            pdm_s = pdm_s - pdm_s / n + pdm
            mdm_s = mdm_s - mdm_s / n + mdm
        if tr_s <= 0:
            continue
        pdi, mdi = 100 * pdm_s / tr_s, 100 * mdm_s / tr_s
        dx = 100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) > 0 else 0.0
        if adx is None:
            dx_hist.append(dx)
            if len(dx_hist) == n:
                adx = sum(dx_hist) / n
        else:
            adx = (adx * (n - 1) + dx) / n
        if adx is not None:
            out[cur["ts"] + H1_S] = adx
    return out


def adx_at(adx: dict, closes: list, t: float):
    """ADX of the last H1 bar that CLOSED at or before `t`."""
    import bisect
    i = bisect.bisect_right(closes, t) - 1
    return adx[closes[i]] if i >= 0 else None


# ── Helpers ──────────────────────────────────────────────────────────────────

def spearman(a, b):
    if len(a) < 3:
        return None
    def ranks(x):
        order = sorted(range(len(x)), key=lambda i: x[i])
        r = [0.0] * len(x)
        for k, i in enumerate(order):
            r[i] = float(k)
        return r
    ra, rb = ranks(a), ranks(b)
    try:
        return statistics.correlation(ra, rb)
    except statistics.StatisticsError:
        return None


def sign_agree(a, b):
    pairs = [(x, y) for x, y in zip(a, b) if x != 0 and y != 0]
    return sum((x > 0) == (y > 0) for x, y in pairs) / len(pairs) if pairs else None


def fmt_proof(p: dict) -> str:
    def f(x, spec):
        return "n/a" if x is None else format(x, spec)
    verdict = "PASS" if p["proven"] else f"fail: {p['refusal']}"
    return (f"n={p['n']} blocks={p['clusters']} mean={f(p['mean_r'], '+.3f')}R "
            f"t={f(p['t'], '.2f')} halves={f(p['r_h1'], '+.3f')}/{f(p['r_h2'], '+.3f')} "
            f"-> {verdict}")


def _ridge():
    from sklearn.linear_model import Ridge
    return Ridge(alpha=1.0)


def model_rows(signals, label_key) -> list[dict]:
    out = []
    for s in signals:
        y = s.get(label_key)
        if y is None:
            continue
        try:
            feats = pad_to_schema(json.loads(s.get("ml_features_json") or ""))
        except (TypeError, ValueError):
            continue
        if feats:
            out.append({"features": feats, "tpl_r": float(y),
                        "trigger_time": float(s["trigger_time"])})
    return out


# ── The report ───────────────────────────────────────────────────────────────

def run(since: float, cost: float, cache: Path) -> list[str]:
    L: list[str] = []
    say = L.append
    sigs = load_signals(since)
    templates = load_templates()
    broker = load_broker()
    say(f"# Reversal measurement audit ({datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC)\n")
    say(f"{len(sigs)} triggered signals since "
        f"{datetime.fromtimestamp(since, timezone.utc):%Y-%m-%d}; cost {cost} points "
        f"a round trip; {len(templates)} templates; {len(broker)} broker results "
        "with an initial risk.\n")

    bars = load_bars([{"created_at": s["trigger_time"]} for s in sigs], cache)
    ts_index = [b.ts for b in bars]
    import bisect

    # Step 2/3: own-template labels.
    refusals: Counter = Counter()
    for s in sigs:
        name = otl.template_name(s["strategy"])
        tpl = templates.get(name) if name else None
        if name and tpl is None:
            s["own_r"], why = None, f"template '{name}' no longer exists"
        else:
            t = float(s["trigger_time"])
            i = bisect.bisect_left(ts_index, t - 120)
            j = bisect.bisect_right(ts_index, t + otl.HORIZON_S + 60)
            window = [{"ts": b.ts, "high": b.high, "low": b.low, "close": b.close}
                      for b in bars[i:j]]
            lab = otl.label(tpl, window, s["direction"], s["trigger_price"], t, cost) \
                if tpl else otl.OwnLabel(None, f"not a template: {s['strategy']}")
            s["own_r"], why = lab.r, lab.refusal
        if s["own_r"] is None:
            refusals[why.split(";")[0][:90]] += 1
        # v9's label as trained today, and corrected with the broker's own
        # lot and initial stop where the trade reached the broker.
        s["v9_r"] = _realised_r(s) if s["status"] == "closed" else None
        b = broker.get(int(s["mt5_ticket"])) if s.get("mt5_ticket") else None
        s["broker_r"] = (b[0] / b[1]) if b else None
        if s["v9_r"] is None:
            s["v9_fixed"] = None
        elif s["live_exec_status"] == "executed":
            s["v9_fixed"] = (float(s["net_pnl_dollars"]) / b[1]
                             if b and s.get("net_pnl_dollars") is not None else None)
        else:
            s["v9_fixed"] = s["v9_r"]

    n_own = sum(s["own_r"] is not None for s in sigs)
    say("## Step 2 — own-template labels\n")
    say(f"Labelled {n_own} of {len(sigs)}. Refused:\n")
    for why, k in refusals.most_common(12):
        say(f"- {k:5d}  {why}")
    say("")

    say("## Step 3a — fidelity against the broker\n")
    ex = [s for s in sigs if s["broker_r"] is not None]
    for key, what in (("own_r", "own-template replay"), ("tpl_r", "single-template replay (tpl_r)"),
                      ("v9_r", "v9 label as trained")):
        pairs = [(s[key], s["broker_r"]) for s in ex if s.get(key) is not None]
        if not pairs:
            say(f"- {what}: no rows")
            continue
        a, b = zip(*pairs)
        sa, rho = sign_agree(a, b), spearman(list(a), list(b))
        say(f"- {what}: n={len(pairs)}, sign agrees "
            f"{'n/a' if sa is None else f'{sa:.0%}'}, rank corr "
            f"{'n/a' if rho is None else f'{rho:.2f}'}, mean {statistics.mean(a):+.3f}R "
            f"vs broker {statistics.mean(b):+.3f}R")
    both = [s for s in ex if s["own_r"] is not None and s["tpl_r"] is not None]
    if both:
        a = [s["own_r"] for s in both]
        b = [s["tpl_r"] for s in both]
        c = [s["broker_r"] for s in both]
        say(f"- same {len(both)} rows: own {sign_agree(a, c):.0%} / "
            f"{spearman(a, c):.2f}, tpl_r {sign_agree(b, c):.0%} / {spearman(b, c):.2f}")
    fx = [s for s in sigs if s["live_exec_status"] == "executed"
          and s["v9_r"] is not None and s["v9_fixed"] is not None]
    if fx:
        ratio = statistics.median(abs(s["v9_r"]) / abs(s["v9_fixed"])
                                  for s in fx if s["v9_fixed"])
        say(f"- v9 label on executed rows: median |trained| / |real R| = "
            f"{ratio:.2f} over {len(fx)} rows")
    say("")

    say("## Step 3b — baseline, own-template label\n")
    own = [s for s in sigs if s["own_r"] is not None]
    say(f"- all: {fmt_proof(em.prove([s['own_r'] for s in own], [s['trigger_time'] for s in own]))}")
    by_t = defaultdict(list)
    for s in own:
        by_t[otl.template_name(s["strategy"])].append(s)
    for name, rows in sorted(by_t.items(), key=lambda kv: -len(kv[1])):
        if len(rows) >= 30:
            say(f"- {name}: {fmt_proof(em.prove([s['own_r'] for s in rows], [s['trigger_time'] for s in rows]))}")
    say("")

    say("## Step 3c — would a model select winners? (walk-forward, local fits)\n")
    say("Each row: the trades a model fitted only on earlier data would have "
        "taken (predicted R >= 0), judged by `edge_model.prove`.\n")
    for label_key, what in (("v9_r", "v9 label as trained (mixed lot)"),
                            ("v9_fixed", "v9 label, real lot and stop"),
                            ("tpl_r", "single-template label (tpl_r)"),
                            ("own_r", "own-template label")):
        rows = model_rows(sigs, label_key)
        for factory, fname in ((em._lgb, "LightGBM"), (_ridge, "ridge")):
            st = em.EdgeModel(factory).fit(rows)
            if not st.fitted:
                say(f"- {what}, {fname}: not fitted: {st.refusal}")
                continue
            p = {"n": st.n_accepted, "clusters": st.clusters, "mean_r": st.mean_r,
                 "t": st.t, "r_h1": st.r_h1, "r_h2": st.r_h2, "proven": st.proven,
                 "refusal": st.refusal}
            say(f"- {what}, {fname} (of {st.n_rows}, AUC {st.auc_oos}): {fmt_proof(p)}")
    say("")

    # Step 4.
    say("## Step 4 — the challengers against the bar set on 2026-09-30\n")
    h1 = h1_from_m1(bars)
    adx = wilder_adx(h1)
    closes = sorted(adx)
    for s in own:
        s["adx"] = adx_at(adx, closes, float(s["created_at"]))
    ranging = [s for s in own if s["adx"] is not None and s["adx"] < RANGING_BELOW]
    trending = [s for s in own if s["adx"] is not None and s["adx"] >= TRENDING_FROM]
    say(f"- cell 2, reversal in a ranging market (ADX < {RANGING_BELOW:g}): "
        f"{fmt_proof(em.prove([s['own_r'] for s in ranging], [s['trigger_time'] for s in ranging]))}")
    say(f"- cell 3, reversal in a trending market (ADX >= {TRENDING_FROM:g}): "
        f"{fmt_proof(em.prove([s['own_r'] for s in trending], [s['trigger_time'] for s in trending]))}")
    say("- cell 4, inside vs outside 60 minutes after high-impact news: NOT "
        "MEASURABLE. `news_proximity_norm` records minutes to the NEXT event, "
        "and the calendar cache holds one week. Nothing stored says a signal "
        "came after news.")

    tpa = DATA_DIR / "trend_pa.db"
    if tpa.exists():
        with _ro(tpa) as c:
            rows = [dict(r) for r in c.execute(
                "SELECT created_at, direction, entry, exit_price, risk, r_net FROM tpa_signals"
                " WHERE origin='backtest' AND status='closed' AND risk > 0"
                " AND exit_price IS NOT NULL ORDER BY created_at")]
        def r_at(r, cst):
            sign = 1.0 if r["direction"] == "BUY" else -1.0
            return ((float(r["exit_price"]) - float(r["entry"])) * sign - cst) / float(r["risk"])
        ts = [float(r["created_at"]) for r in rows]
        say(f"- cell 1, Trend PA backtest at its own 0.30 cost: "
            f"{fmt_proof(em.prove([r_at(r, 0.30) for r in rows], ts))}")
        say(f"- cell 1, Trend PA backtest at the bar's {cost} cost: "
            f"{fmt_proof(em.prove([r_at(r, cost) for r in rows], ts))}")
    say("")
    return L


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", default=str(DATA_DIR / "research_m1_cache"))
    ap.add_argument("--cost", type=float, default=DEFAULT_COST)
    ap.add_argument("--since", default="2026-07-20")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    t0 = time.time()
    lines = run(since, args.cost, cache)
    lines.append(f"_{time.time() - t0:.0f}s_")
    text = "\n".join(lines)
    print(text)
    if args.out:
        Path(args.out).write_text(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
