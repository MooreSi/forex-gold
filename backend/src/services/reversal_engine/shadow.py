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
    # gate's own threshold is 0.0 (_ML_BLOCK_THRESHOLD).
    min_ml_prob: float = 0.0
    # None = this variant does not consult the meta-labeller.
    meta_threshold: Optional[float] = None
    require_trigger: bool = False
    respect_liquidity: bool = False
    is_champion: bool = False


# The champion must be in this list or there is nothing to compare against,
# and a table of challengers alone proves nothing.
DEFAULT_VARIANTS: tuple[Variant, ...] = (
    Variant("live (champion)", min_ml_prob=0.0, is_champion=True),
    Variant("ML floor 0.50", min_ml_prob=0.5),
    Variant("confirmed entries", require_trigger=True),
    Variant("liquidity aware", respect_liquidity=True),
    Variant("meta 0.55", meta_threshold=0.55),
)


def decide(variant: Variant, ctx: dict) -> tuple[bool, str]:
    """`(would_take, reason)` for one variant, from already-computed facts.

    An unavailable input never refuses. A challenger that blocked on a
    model which has not been fitted would report a refusal rate that says
    nothing about the model, and it would be read as though it did.
    """
    ml = ctx.get("ml_prob")
    if ml is not None and float(ml) < variant.min_ml_prob:
        return False, f"ML {float(ml):.3f} below floor {variant.min_ml_prob:.2f}"

    if variant.meta_threshold is not None:
        p = ctx.get("meta_prob")
        if p is not None and float(p) < variant.meta_threshold:
            return False, (f"meta-label {float(p):.3f} below "
                           f"{variant.meta_threshold:.2f}")

    if variant.require_trigger and ctx.get("trigger_passed") is False:
        return False, "level not confirmed"

    if variant.respect_liquidity and ctx.get("liquidity_blocked"):
        return False, "inside a liquidity window"

    return True, ""


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


def report(variants=DEFAULT_VARIANTS) -> list[dict]:
    """Each variant's realised expectancy over the trades it would have
    taken.

    `mean_r` is None, never 0.0, for a variant with nothing to score. Zero
    expectancy and no evidence are different statements and a table that
    renders both as 0.000 invites the wrong one to be acted on.
    """
    rows = shadow_repo.closed_decisions()

    acc: dict[str, dict] = {v.name: {"variant": v.name,
                                     "is_champion": v.is_champion,
                                     "n_taken": 0, "n_skipped": 0,
                                     "net": 0.0, "_r": 0.0, "_n_r": 0}
                            for v in variants}
    for r in rows:
        bucket = acc.get(r["variant"])
        if bucket is None:
            continue
        if not r["would_take"]:
            bucket["n_skipped"] += 1
            continue
        bucket["n_taken"] += 1
        bucket["net"] += float(r["net"] or 0.0)
        sl = float(r["sl_dist"] or 0.0)
        if sl > 0:
            bucket["_r"] += float(r["pnl_pts"] or 0.0) / sl
            bucket["_n_r"] += 1

    out = []
    for b in acc.values():
        n_r = b.pop("_n_r")
        total_r = b.pop("_r")
        b["mean_r"] = (total_r / n_r) if n_r else None
        out.append(b)
    return out
