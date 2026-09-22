"""Set & Forget section's API.

Forwards, and nothing else. The reading of the chart, the rules that build a
candidate and the re-validation of whatever the model sends back all live in
`services/setforget/`; this file names the three operations the section can
perform and hands each to one service.

**Nothing here places an order.** The section's Execute button posts to the
existing money endpoints in `api/routers/orders.py` -- the same two the manual
Market order and Limit order dialogs use -- so no second order path exists to
drift from the first.
"""
from __future__ import annotations

from typing import Any, Optional

from backend.src.services.risk import settings as _risk
from backend.src.services.setforget import analysis as _analysis
from backend.src.services.setforget import setup as _setup

__all__ = [
    "read_chart", "public_evidence", "propose", "evaluate", "score",
    "invalidations",
    "lot_from_risk", "money_at_risk", "money_at_target",
    "get_risk_settings", "update_risk_settings",
    "MIN_RR", "PREFERRED_RR",
]

# Re-exported so the router reports the method's own floor rather than
# restating the number and drifting from it.
MIN_RR = _setup.MIN_RR
PREFERRED_RR = _setup.PREFERRED_RR


async def read_chart(engine: Any) -> dict:
    """The measured evidence. No model, nothing billed."""
    return await _analysis.gather(engine)


def public_evidence(evidence: dict) -> dict:
    """The evidence with the candle series taken out, ready for the browser."""
    return _analysis.public_evidence(evidence)


def propose(evidence: dict) -> tuple[Optional[dict], str]:
    """The candidate the rules produce, or None and why there is none."""
    return _analysis.propose(evidence)


async def evaluate(engine: Any, cfg: dict, timeout: int = 60) -> dict:
    """Read the chart and have the configured model judge the candidate.

    REACHES AN EXTERNAL API AND COSTS MONEY PER CALL, but only when there is a
    candidate to judge -- see `analysis.evaluate`.
    """
    return await _analysis.evaluate(engine, cfg, timeout=timeout)


def score(evidence: dict, candidate: Optional[dict]) -> dict:
    """The confluence checklist for this candidate. Pure; touches nothing."""
    return _analysis.score(evidence, candidate)


def invalidations(candidate: Optional[dict]) -> list[str]:
    """Every rule this candidate breaks, in the operator's words."""
    return _setup.invalidations(candidate)


def lot_from_risk(entry: float, stop_loss: float, balance: float,
                  risk_pct: float) -> Optional[float]:
    """The lot size for a percentage of the balance over this stop distance."""
    return _setup.lot_from_risk(entry, stop_loss, balance, risk_pct)


def money_at_risk(candidate: dict, lots: Optional[float]) -> Optional[float]:
    return _setup.money_at_risk(candidate, lots)


def money_at_target(candidate: dict, lots: Optional[float]) -> Optional[float]:
    return _setup.money_at_target(candidate, lots)


def get_risk_settings() -> dict:
    return _risk.get()


def update_risk_settings(fields: dict) -> None:
    _risk.update(fields)
