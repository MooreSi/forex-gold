"""One command that turns the engine's own history into numbers.

Phase 1 of `docs/todo/reversal-engine/200`, assembled. Nothing here places,
modifies or closes anything; it reads history, writes two measurement
columns and returns a report.

    reconstruct excursion -> measure execution cost -> attribute by cohort
    -> fit barriers -> sweep exit policies

The order is not arbitrary. Each step is the input to the next: barriers
cannot be fitted without excursion, and the fit is not worth acting on
until the cost it has to clear is measured rather than assumed.

**Every step degrades rather than fails.** A broker that no longer serves
ticks that far back, a bridge that is down, a sample too small to fit on --
each of those produces a report that says so and a study that still returns
the parts it could compute. A research tool that raises halfway through is
a research tool nobody runs twice.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from backend.src.services.broker import tca, tca_repo
from backend.src.services.market import (barrier_fit, correlation,
                                          exit_replay, order_flow)
from backend.src.services.market import price_path as pp
from backend.src.services.reversal_engine import attribution, excursion_backfill
from backend.src.services.reversal_engine import macro_backfill, measure_repo

log = logging.getLogger("reversal_engine")

RE_STRATEGY_HINT = "reversal"
# Replaying an exit policy needs the whole tick path of every trade in the
# sample, which is one bridge round trip each. Bounded so a study is minutes
# rather than hours; raise it deliberately when the answer matters more than
# the wait.
SWEEP_SAMPLE = 60
DEFAULT_STOPS = (2.0, 3.0, 4.0, 5.0, 6.0, 8.0)
DEFAULT_TARGETS = (3.0, 4.0, 6.0, 8.0, 12.0)


async def probe(bridge, now: Optional[float] = None) -> dict:
    """What this broker's feed can actually answer, before anything relies
    on it. Section 4.3: "the feed has no trade side" and "the market was
    quiet" produce the same empty delta."""
    now = now or time.time()
    depth = await excursion_backfill.probe_tick_history(bridge, now)
    ticks = []
    try:
        ticks = await bridge.get_ticks_range(now - 3600.0, now) or []
    except Exception:                             # noqa: BLE001
        pass
    cap = order_flow.probe_feed(ticks)
    return {"tick_history_by_days_back": depth,
            "feed": {"n_ticks_last_hour": cap.n_ticks,
                     "has_trade_side": cap.has_trade_side,
                     "has_volume": cap.has_volume,
                     "note": cap.note}}


async def measure_costs(bridge, limit: int = 500) -> dict:
    """Price the round trip of every closed trade that has no cost row.

    The requested price is the middle of the signal's stated entry zone
    when it has one, because that is the price the decision was made at.
    Falling back to the fill itself would measure a slippage of exactly
    zero on every trade, which is worse than not measuring: it looks like
    an answer.
    """
    rows = tca_repo.trades_awaiting_cost_measurement(limit)
    measured = unmeasured = 0
    for t in rows:
        lo = float(t.get("entry_low") or 0.0)
        hi = float(t.get("entry_high") or 0.0)
        requested = (lo + hi) / 2.0 if lo > 0 and hi > 0 else 0.0
        if requested <= 0:
            continue
        sl = abs(float(t.get("entry_price") or 0.0)
                 - float(t.get("stop_loss") or 0.0))
        try:
            cost = await tca.measure(bridge, t, requested_price=requested,
                                     sl_dist=sl,
                                     bucket=str(t.get("strategy") or ""))
        except Exception as e:                    # noqa: BLE001
            log.debug("tca: %s could not be measured: %s", t.get("trade_id"), e)
            continue
        tca_repo.record_fill_cost(cost, mt5_ticket=t.get("mt5_ticket"),
                                  strategy=str(t.get("strategy") or ""),
                                  sl_dist=sl)
        measured += 1 if cost.measured else 0
        unmeasured += 0 if cost.measured else 1
    mean_r, n = tca_repo.mean_cost_r()
    return {"considered": len(rows), "measured": measured,
            "no_ticks": unmeasured, "mean_cost_r": mean_r, "n_costed": n}


async def _paths_for(bridge, rows, limit: int) -> list[dict]:
    out = []
    for row in rows[:limit]:
        entry = float(row.get("trigger_price") or 0.0)
        t0 = float(row.get("trigger_time") or 0.0)
        t1 = float(row.get("close_time") or 0.0)
        if entry <= 0 or t0 <= 0 or t1 <= t0:
            continue
        direction = str(row.get("direction") or "BUY")
        try:
            ticks = await bridge.get_ticks_range(t0, min(t1, t0 + 86_400.0))
        except Exception:                         # noqa: BLE001
            continue
        path = pp.build_tick_path(ticks or [], direction)
        if path:
            out.append({"path": path, "entry": entry, "direction": direction,
                        "close_time": t1})
    return out


async def run_study(bridge, backfill_limit: int = 500,
                    cost_limit: int = 500,
                    sweep_sample: int = SWEEP_SAMPLE) -> dict:
    """The whole phase-1 study. Returns a dict; `render` turns it into text."""
    report: dict = {"ran_at": time.time()}

    report["probe"] = await probe(bridge)
    # Cross-asset context (section 5.9). Reported rather than acted on: the
    # correlated-exposure cap is a sizing decision and sizing decisions are
    # not made by a research report.
    try:
        report["cross_asset"] = await correlation.snapshot(bridge)
    except Exception:                             # noqa: BLE001
        report["cross_asset"] = {"symbols": [], "correlations": {}}
    report["backfill"] = (await excursion_backfill.backfill(
        bridge, backfill_limit)).__dict__
    report["costs"] = await measure_costs(bridge, cost_limit)

    closed = measure_repo.closed_executed_rows()
    report["attribution"] = attribution.render(attribution.cohorts(closed))
    report["n_closed"] = len(closed)

    # How much of the training set still carries constant macro values.
    # Dry run only: rewriting stored vectors changes what the live ML gate
    # learns at its next retrain, so it is a decision, not a side effect of
    # running a report. See macro_backfill and section 4.4.
    report["macro"] = macro_backfill.backfill(
        measure_repo.training_vectors(), {}, write_fn=lambda _x: None)

    obs = measure_repo.excursion_observations()
    fit = barrier_fit.fit_barriers(obs)
    report["barrier_fit"] = fit.__dict__
    report["n_excursions"] = len(obs)

    paths = await _paths_for(bridge, closed, sweep_sample)
    report["n_paths"] = len(paths)
    if paths:
        cost_r = report["costs"].get("mean_cost_r") or 0.0
        # Cost enters the sweep in POINTS, because that is what the replay
        # charges against the stop it is testing. Using the mean cost in R
        # would charge the same fraction whatever stop width is being
        # evaluated, which is precisely the comparison the sweep exists to
        # make: a tighter stop pays proportionally more.
        mean_sl = sum(abs(float(r.get("sl_dist") or 0.0)) for r in closed[:len(paths)])
        mean_sl = mean_sl / len(paths) if paths else 0.0
        report["sweep"] = [r.__dict__ for r in barrier_fit.sweep(
            paths, DEFAULT_STOPS, DEFAULT_TARGETS,
            cost_pts=cost_r * mean_sl, bootstrap=1000)[:10]]
        report["sweep_no_breakeven_vs_breakeven"] = _breakeven_penalty(
            paths, cost_r * mean_sl)
    return report


def _breakeven_penalty(paths, cost_pts: float) -> dict:
    """What a breakeven move costs, at several stop widths.

    Item 030's suspect, priced. `tools/exit_policy_lab.py` already found a
    penalty in 8 of 8 configurations on the main trading path; this asks the
    same question of the reversal engine's own trades.
    """
    out = {}
    for stop in (3.0, 4.0, 5.0):
        target = stop * 2
        without = barrier_fit.expectancy(paths, exit_replay.ExitPolicy(
            stop_pts=stop, target_pts=target, cost_pts=cost_pts))
        with_be = barrier_fit.expectancy(paths, exit_replay.ExitPolicy(
            stop_pts=stop, target_pts=target, be_trigger_pts=stop,
            cost_pts=cost_pts))
        out[f"stop {stop:g} / target {target:g}"] = round(with_be - without, 4)
    return out


def render(report: dict) -> str:
    """The study as text, for a log, an email or a panel."""
    lines = ["Reversal engine research study",
             f"  closed live trades: {report.get('n_closed', 0)}",
             f"  with an excursion:  {report.get('n_excursions', 0)}",
             ""]

    feed = report.get("probe", {}).get("feed", {})
    if feed:
        lines += ["feed", f"  {feed.get('note', '')}",
                  f"  tick history by days back: "
                  f"{report['probe'].get('tick_history_by_days_back')}", ""]

    bf = report.get("backfill") or {}
    if bf:
        lines += ["excursion backfill",
                  f"  {bf.get('measured', 0)} measured, "
                  f"{bf.get('no_coverage', 0)} with no tick coverage, "
                  f"{bf.get('failed', 0)} failed", ""]

    c = report.get("costs") or {}
    if c:
        mean = c.get("mean_cost_r")
        lines += ["execution cost",
                  f"  {c.get('measured', 0)} newly measured, "
                  f"{c.get('n_costed', 0)} costed in total",
                  f"  mean round-trip cost: "
                  + ("not yet measurable" if mean is None else f"{mean:.3f}R"),
                  ""]

    fit = report.get("barrier_fit") or {}
    if fit.get("refusal"):
        lines += ["fitted barriers", f"  {fit['refusal']}", ""]
    elif fit:
        lines += ["fitted barriers",
                  f"  stop   {fit.get('stop_pts')} pts "
                  f"({fit.get('stop_atr_mult')} x ATR)",
                  f"  target {fit.get('target_pts')} pts "
                  f"({fit.get('target_atr_mult')} x ATR)",
                  f"  implied R:R {fit.get('rr')} from "
                  f"{fit.get('n_winners')} winners", ""]

    sweep = report.get("sweep") or []
    if sweep:
        lines.append("exit policy sweep (net of measured cost, best first)")
        lines.append(f"  {'stop':>6}{'target':>8}{'R':>9}{'95% low':>10}"
                     f"{'95% high':>10}{'1st half':>10}{'2nd half':>10}")
        for r in sweep:
            lines.append(
                f"  {r['stop_pts']:>6.1f}{r['target_pts']:>8.1f}"
                f"{r['expectancy']:>+9.3f}{r['ci_low']:>+10.3f}"
                f"{r['ci_high']:>+10.3f}{r['first_half']:>+10.3f}"
                f"{r['second_half']:>+10.3f}")
        lines.append("")

    be = report.get("sweep_no_breakeven_vs_breakeven") or {}
    if be:
        lines.append("what a breakeven move costs (negative = it costs money)")
        for k, v in be.items():
            lines.append(f"  {k}: {v:+.3f}R")
        lines.append("")

    ca = report.get("cross_asset") or {}
    if ca.get("correlations"):
        lines.append("cross-asset correlation (H1 returns)")
        for pair, rho in sorted(ca["correlations"].items()):
            lines.append(f"  {pair}: {rho:+.3f}")
        lines.append("")

    m = report.get("macro") or {}
    if m.get("considered"):
        lines += ["macro coverage",
                  f"  {m.get('considered', 0)} stored training vectors, "
                  f"{m.get('no_data', 0) + m.get('would_update', 0)} still "
                  f"carrying the five macro features as constants",
                  "  (repairing them is a separate, deliberate action: it "
                  "changes what the ML gate learns)", ""]

    if report.get("attribution"):
        lines += ["attribution", report["attribution"], ""]

    return "\n".join(lines).rstrip()
