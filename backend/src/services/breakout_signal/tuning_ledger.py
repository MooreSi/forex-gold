"""The Breakout engine's tuning experiment ledger (docs/todo/007).

The batch review (`_run_batch_analysis`, every 10 closed signals) asks the AI
for parameter adjustments. This module is where they go.

RECORD mode (default, `tuning_approval_required` = "0"): every adjustment is
applied as it arrives, exactly as before this existed, through the same
`adaptive_params.apply_adjustment` call. The ledger writes each one down,
marks `concurrent` > 1 when its batch changed several params at once (so no
single change can be credited or blamed), captures the baseline and, after
MIN_SAMPLE closed signals, records a verdict. It never rolls back: record
mode changes nothing about how the engine trades.

APPROVE mode ("1"): adjustments become proposals and change nothing. A newer
batch supersedes older proposals. The owner approves one; only one may run
at a time, so every change is judged on its own. On approval it is applied
and the baseline captured (mean net $ of the last MIN_SAMPLE closed signals).
After MIN_SAMPLE closed signals:
  worse than baseline -> rolled back to the old value (verdict "worse")
  otherwise           -> kept, and it is simply the engine's value now
The failure line (net since applied <= -FAILURE_USD) rolls back early. If the
param no longer holds the experiment's value, a human changed it: the
experiment is abandoned and the human's value is left alone.

"Not worse" keeps. There is no significance test and 30 signals is a noisy
sample; the value of the ledger is one change at a time, a written reason, a
baseline, and an undo -- not proof.

Every write to a param goes through `apply_adjustment`, so its clamps and
`tuner_locked` refusals hold for approvals and rollbacks too. Nothing here
places, closes or sizes an order.
"""
from __future__ import annotations

import logging
import time
from statistics import fmean
from typing import Optional

from backend.src.services.breakout_signal import adaptive_params as ap
from backend.src.services.breakout_signal import breakout_signal_repo as bdb
from backend.src.services.breakout_signal import tuning_ledger_repo as repo

_log = logging.getLogger("breakout_signal")

MODE_KEY = "tuning_approval_required"
MIN_SAMPLE = 30          # closed signals before a verdict (owner decision, provisional)
FAILURE_USD = 100.0      # early rollback line on the virtual $1,000 account (provisional)

# Statuses. `applied_auto` and `judged` are record mode's; the rest approve mode's.
PROPOSED, SUPERSEDED, REJECTED = "proposed", "superseded", "rejected"
RUNNING, KEPT, ROLLED_BACK, ABANDONED = "running", "kept", "rolled_back", "abandoned"
APPLIED_AUTO, JUDGED = "applied_auto", "judged"


def approval_required() -> bool:
    return bdb.get_config(MODE_KEY, "0") == "1"


def set_approval_required(on: bool) -> None:
    bdb.set_config(MODE_KEY, "1" if on else "0")
    _log.info("[BO-Tuning] approval %s", "required" if on else "not required (record mode)")


def _baseline(at: float) -> tuple[int, Optional[float]]:
    nets = repo.closed_net_before(at, MIN_SAMPLE)
    return len(nets), (round(fmean(nets), 4) if nets else None)


def _preview(param: str, value) -> Optional[float]:
    """The value `apply_adjustment` would store, or None if it would refuse or
    change nothing. Mirrors its checks; it stays the only writer."""
    meta = ap.PARAMS.get(param)
    if meta is None or meta.get("tuner_locked"):
        return None
    try:
        clamped = round(min(meta["max"], max(meta["min"], float(value))), 4)
    except (TypeError, ValueError):
        return None
    if abs(clamped - ap.get(param)) < 1e-6:
        return None
    return clamped


# ── The batch review's hook ─────────────────────────────────────────────────

def handle_batch(adjustments: list[dict], summary: str = "") -> list[str]:
    """Take a batch's adjustments. Returns the "param→value" strings that were
    APPLIED, the same list the batch review logged before this existed."""
    if approval_required():
        _propose(adjustments, summary)
        return []
    return _apply_and_record(adjustments, summary)


def _apply_and_record(adjustments: list[dict], summary: str) -> list[str]:
    applied: list[str] = []
    changes: list[tuple[str, float, float, str]] = []
    for adj in adjustments:
        param = adj.get("param", "")
        value = adj.get("new_value")
        reason = adj.get("reason", "")
        if param and value is not None:
            old = ap.get(param) if param in ap.PARAMS else None
            new_v = ap.apply_adjustment(param, float(value), reason)
            if new_v is not None:
                applied.append(f"{param}→{new_v:.4g}")
                changes.append((param, old, new_v, reason))
    try:
        now = time.time()
        base_n, base_mean = _baseline(now)
        for param, old, new_v, reason in changes:
            repo.insert(param, new_v, APPLIED_AUTO, old_value=old, hypothesis=reason,
                        summary=summary, concurrent=len(changes), applied_at=now,
                        baseline_n=base_n, baseline_mean=base_mean)
    except Exception as exc:
        _log.warning("[BO-Tuning] could not record applied changes: %s", exc)
    return applied


def _propose(adjustments: list[dict], summary: str) -> None:
    for old in repo.with_status(PROPOSED):
        repo.update(old["id"], status=SUPERSEDED, decided_at=time.time())
    for adj in adjustments:
        param = adj.get("param", "")
        value = _preview(param, adj.get("new_value"))
        if value is None:
            continue
        repo.insert(param, value, PROPOSED, old_value=ap.get(param),
                    hypothesis=adj.get("reason", ""), summary=summary)
        _log.info("[BO-Tuning] proposed %s: %.4g -> %.4g", param, ap.get(param), value)


# ── Owner decisions ─────────────────────────────────────────────────────────

def approve(exp_id: int) -> dict:
    exp = repo.get(exp_id)
    if not exp or exp["status"] != PROPOSED:
        raise ValueError(f"Experiment {exp_id} is not a waiting proposal.")
    if repo.with_status(RUNNING):
        raise ValueError("An experiment is already running. One change at a time.")
    old = ap.get(exp["param"])
    new_v = ap.apply_adjustment(exp["param"], float(exp["new_value"]),
                                f"approved experiment #{exp_id}: {exp['hypothesis'] or ''}")
    if new_v is None:
        repo.update(exp_id, status=REJECTED, decided_at=time.time(),
                    verdict="no_change")
        raise ValueError(f"{exp['param']} is already {old:.4g}; nothing to test.")
    now = time.time()
    base_n, base_mean = _baseline(now)
    repo.update(exp_id, status=RUNNING, old_value=old, new_value=new_v, applied_at=now,
                baseline_n=base_n, baseline_mean=base_mean)
    return repo.get(exp_id)


def reject(exp_id: int) -> dict:
    exp = repo.get(exp_id)
    if not exp or exp["status"] != PROPOSED:
        raise ValueError(f"Experiment {exp_id} is not a waiting proposal.")
    repo.update(exp_id, status=REJECTED, decided_at=time.time())
    return repo.get(exp_id)


# ── Judging ─────────────────────────────────────────────────────────────────

def _progress(exp: dict) -> tuple[int, Optional[float], float]:
    nets = repo.closed_net_since(float(exp["applied_at"]))
    return len(nets), (round(fmean(nets), 4) if nets else None), round(sum(nets), 2)


def _worse(after_mean: Optional[float], base_mean: Optional[float]) -> bool:
    return base_mean is not None and after_mean is not None and after_mean < base_mean


def _finish(exp: dict, status: str, verdict: str, n: int, mean, total) -> None:
    repo.update(exp["id"], status=status, verdict=verdict, decided_at=time.time(),
                after_n=n, after_mean=mean, after_sum=total)
    _log.info("[BO-Tuning] #%d %s %s -> %s (%s; n=%d mean=%s base=%s)",
              exp["id"], exp["param"], exp["new_value"], status, verdict, n, mean,
              exp["baseline_mean"])


def _roll_back(exp: dict, verdict: str, n: int, mean, total) -> None:
    ap.apply_adjustment(exp["param"], float(exp["old_value"]),
                        f"rollback experiment #{exp['id']} ({verdict})")
    _finish(exp, ROLLED_BACK, verdict, n, mean, total)


def evaluate() -> None:
    """Judge whatever has enough closed signals. Called after each close."""
    for exp in repo.with_status(RUNNING):
        n, mean, total = _progress(exp)
        if abs(ap.get(exp["param"]) - float(exp["new_value"])) > 1e-6:
            _finish(exp, ABANDONED, "changed_by_hand", n, mean, total)
        elif total <= -FAILURE_USD:
            _roll_back(exp, "failure_line", n, mean, total)
        elif n >= MIN_SAMPLE:
            if _worse(mean, exp["baseline_mean"]):
                _roll_back(exp, "worse", n, mean, total)
            else:
                _finish(exp, KEPT, "not_worse", n, mean, total)
    for exp in repo.with_status(APPLIED_AUTO):
        n, mean, total = _progress(exp)
        if n >= MIN_SAMPLE:
            base = exp["baseline_mean"]
            verdict = ("no_baseline" if base is None else
                       "worse" if mean < base else "better" if mean > base else "same")
            _finish(exp, JUDGED, verdict, n, mean, total)


# ── The dashboard's read ────────────────────────────────────────────────────

def state() -> dict:
    running = repo.with_status(RUNNING)
    run = None
    if running:
        run = dict(running[0])
        run["after_n"], run["after_mean"], run["after_sum"] = _progress(run)
    return {
        "approval_required": approval_required(),
        "min_sample": MIN_SAMPLE,
        "failure_usd": FAILURE_USD,
        "running": run,
        "proposals": repo.with_status(PROPOSED),
        "history": repo.recent(exclude=(PROPOSED, RUNNING)),
    }


# ── Off the event loop, for the API ─────────────────────────────────────────

async def state_async() -> dict:
    from backend.src.db.database import to_db_thread
    return await to_db_thread(state)


async def approve_async(exp_id: int) -> dict:
    from backend.src.db.database import to_db_thread
    return await to_db_thread(approve, exp_id)


async def reject_async(exp_id: int) -> dict:
    from backend.src.db.database import to_db_thread
    return await to_db_thread(reject, exp_id)


async def set_approval_required_async(on: bool) -> None:
    from backend.src.db.database import to_db_thread
    await to_db_thread(set_approval_required, on)
