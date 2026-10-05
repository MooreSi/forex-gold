"""Champion and challenger: what the other configuration would have done.

Section 5.7 of `docs/todo/reversal-engine/200`. Today a model or a template
goes live and the evidence arrives afterwards: v9 shipped on a Saturday and
had its worst Asian session on the next trading day, with nothing to
compare it against.

A shadow decision is derived from facts the live path has **already
computed** -- the ML probability it scored, whether the entry trigger
confirmed, whether a liquidity window was open. Not by re-running the gates
against a second market read. Two reasons, and both matter: a second read
is a different moment and would answer a different question, and a shadow
that makes its own bridge calls costs latency on the live path it exists to
shadow.

Recording never raises. This sits on the order path, and a measurement must
never cost a trade its execution.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from backend.src.services.reversal_engine import shadow_repo

log = logging.getLogger("reversal_engine")


@dataclass(frozen=True)
class Variant:
    name: str
    # Below this predicted R-multiple the variant stands aside. The live
    # gate's own threshold is 0.0 (_ML_BLOCK_THRESHOLD). None = this variant
    # has no ML opinion at all: a challenger that measures some OTHER gate
    # must not also carry the champion's floor, or it is identical to the
    # champion on every signal the ML gate skipped and measures nothing.
    min_ml_prob: Optional[float] = None
    # None = this variant does not consult the meta-labeller.
    meta_threshold: Optional[float] = None
    require_trigger: bool = False
    respect_liquidity: bool = False
    is_champion: bool = False


# The champion must be in this list or there is nothing to compare against,
# and a table of challengers alone proves nothing.
DEFAULT_VARIANTS: tuple[Variant, ...] = (
    Variant("live (champion)", min_ml_prob=0.0, is_champion=True),
    Variant("no filter"),
    Variant("ML floor 0.50", min_ml_prob=0.5),
    Variant("trigger confirmed", require_trigger=True),
    Variant("liquidity aware", respect_liquidity=True),
    Variant("meta 0.55", meta_threshold=0.55),
)


def decide(variant: Variant, ctx: dict) -> tuple[bool, str]:
    """`(would_take, reason)` for one variant, from already-computed facts.

    An unavailable input never refuses. A challenger that blocked on a
    model which has not been fitted would report a refusal rate that says
    nothing about the model, and it would be read as though it did.

    A refusal reports EVERY gate that refused, not only the first: the ledger
    shows the reason beside the call, and one gate masking another hides which
    of them a variant is actually measuring.
    """
    why: list[str] = []
    ml = ctx.get("ml_prob")
    if (variant.min_ml_prob is not None and ml is not None
            and float(ml) < variant.min_ml_prob):
        why.append(f"ML {float(ml):.3f} below floor {variant.min_ml_prob:.2f}")

    if variant.meta_threshold is not None:
        p = ctx.get("meta_prob")
        if p is not None and float(p) < variant.meta_threshold:
            why.append(f"meta-label {float(p):.3f} below "
                       f"{variant.meta_threshold:.2f}")

    if variant.require_trigger and ctx.get("trigger_passed") is False:
        why.append("level not confirmed")

    if variant.respect_liquidity and ctx.get("liquidity_blocked"):
        why.append("inside a liquidity window")

    return (False, "; ".join(why)) if why else (True, "")


def _insert(signal_ref: str, variant: str, would_take: bool,
            reason: str) -> None:
    shadow_repo.insert_decision(signal_ref, variant, would_take, reason)


def record_all(signal_ref: str, ctx: dict, variants=DEFAULT_VARIANTS) -> None:
    """One row per variant. INSERT OR IGNORE, so a retried fill attempt
    cannot double-count."""
    if not signal_ref:
        return
    for v in variants:
        try:
            take, reason = decide(v, ctx)
            _insert(signal_ref, v.name, take, reason)
        except Exception as e:                    # noqa: BLE001
            log.debug("[RE-Engine] shadow record failed for %s: %s", v.name, e)
            return


def decisions_for(signal_ref: str) -> list[dict]:
    return shadow_repo.decisions_for(signal_ref)


# Dollars of P&L per point per 0.1 lot of gold: the virtual ledger sizes every
# signal at 0.1 lot, so net / (sl_dist * this) is the whole trade in R.
_USD_PER_POINT = 10.0


def r_full(row: dict) -> Optional[float]:
    """The whole trade in R, or None when it cannot be stated honestly.

    Read from the dollars the trade banked, not from the last leg's points: a
    ladder win that banked $21 on a 7pt stop is 0.30R, and its last leg moved
    0.1pt ("0.02R"). A broker-executed trade is at the real lot, so its dollars
    are not at 0.1 lot and the division would be wrong -- None, not a wrong
    number. An unsettled signal is None too, never 0.
    """
    if row.get("status") != "closed" or row.get("net") is None:
        return None
    if row.get("live_exec_status") == "executed":
        return None
    sl = float(row.get("sl_dist") or 0.0)
    if sl <= 0:
        return None
    return float(row["net"]) / (sl * _USD_PER_POINT)


def _legacy_r(row: dict) -> Optional[float]:
    sl = float(row.get("sl_dist") or 0.0)
    if row.get("status") != "closed" or sl <= 0:
        return None
    return float(row.get("pnl_pts") or 0.0) / sl


def _live_names(variants) -> set:
    return {v.name for v in variants}


def report(variants=DEFAULT_VARIANTS) -> list[dict]:
    """Each variant's realised expectancy over the trades it would have
    taken, and what it avoided by skipping.

    `mean_r` and its siblings are None, never 0.0, for a variant with nothing
    to score. Zero expectancy and no evidence are different statements and a
    table that renders both as 0.000 invites the wrong one to be acted on.

    `avoided_net` is the dollar result of the signals the variant skipped: a
    skip on a loser is the variant being right, and "taken" alone cannot show
    it. `delta_vs_champion` is the variant's net minus the champion's.
    """
    rows = shadow_repo.closed_decisions()

    acc: dict[str, dict] = {v.name: {"variant": v.name,
                                     "is_champion": v.is_champion,
                                     "n_taken": 0, "n_skipped": 0,
                                     "net": 0.0, "avoided_net": 0.0,
                                     "_r": 0.0, "_n_r": 0,
                                     "_rf": 0.0, "_n_rf": 0}
                            for v in variants}
    for r in rows:
        bucket = acc.get(r["variant"])
        if bucket is None:
            continue
        if not r["would_take"]:
            bucket["n_skipped"] += 1
            bucket["avoided_net"] += float(r["net"] or 0.0)
            continue
        bucket["n_taken"] += 1
        bucket["net"] += float(r["net"] or 0.0)
        legacy = _legacy_r({**r, "status": "closed"})
        if legacy is not None:
            bucket["_r"] += legacy
            bucket["_n_r"] += 1
        full = r_full({**r, "status": "closed"})
        if full is not None:
            bucket["_rf"] += full
            bucket["_n_rf"] += 1

    champ_net = next((b["net"] for b in acc.values() if b["is_champion"]), 0.0)
    out = []
    for b in acc.values():
        n_r, total_r = b.pop("_n_r"), b.pop("_r")
        n_rf, total_rf = b.pop("_n_rf"), b.pop("_rf")
        b["mean_r"] = (total_r / n_r) if n_r else None
        b["mean_r_full"] = (total_rf / n_rf) if n_rf else None
        b["mean_net"] = (b["net"] / b["n_taken"]) if b["n_taken"] else None
        b["delta_vs_champion"] = b["net"] - champ_net
        out.append(b)
    return out


def history(limit: int = 100) -> list[dict]:
    """Every live variant's call, newest first -- one row per decision.

    `ledger()` is the one-row-per-signal view the dashboard shows; this stays
    for callers that want the flat decisions. Decisions by a variant that has
    since been retired are not returned: they would sit in the table as a
    column nothing is measuring any more.

    A pass-through to the repo for the SQL, deliberately: a controller may not
    reach a repo directly.
    """
    live = _live_names(DEFAULT_VARIANTS)
    out = []
    for r in shadow_repo.recent_decisions(limit):
        if r["variant"] not in live:
            continue
        r["r_full"] = r_full(r)
        r["r_replay"] = r.get("tpl_r")
        out.append(r)
    return out


def ledger(limit: int = 50) -> list[dict]:
    """The virtual trade ledger: ONE row per signal, newest first.

    A signal's result is stated once and each variant's call sits beside it.
    The flat per-decision table repeated one trade's dollars once per variant,
    and the owner read the same "-$558.40" down a column as invented figures
    (2026-10-02). `limit` counts signals, not decision rows.
    """
    live = _live_names(DEFAULT_VARIANTS)
    rows = shadow_repo.ledger_rows(limit)
    by_ref: dict[str, dict] = {}
    for r in rows:
        if r["variant"] not in live:
            continue
        sig = by_ref.get(r["signal_ref"])
        if sig is None:
            closed = r["status"] == "closed"
            sig = by_ref[r["signal_ref"]] = {
                "signal_ref": r["signal_ref"], "ts": r["ts"],
                "direction": r["direction"], "status": r["status"],
                "outcome": r["outcome"],
                "net": r["net"] if closed else None,
                "r": _legacy_r(r), "r_full": r_full(r),
                "r_replay": r.get("tpl_r") if closed else None,
                "executed": r.get("live_exec_status") == "executed",
                "calls": {},
            }
        sig["ts"] = max(sig["ts"], r["ts"])
        sig["calls"][r["variant"]] = {"take": bool(r["would_take"]),
                                      "reason": r["reason"] or ""}
    return sorted(by_ref.values(), key=lambda x: x["ts"], reverse=True)
