"""Set & Forget — the Alex G swing method, as a section of the Trading tab.

**No endpoint in this module places, closes or modifies anything.** That is the
design, not an accident of what has been built so far. The section's Execute
button posts to `api/routers/orders.py` — the same `/orders/market` and
`/orders/limit` the manual dialogs use — so there is exactly one order path in
this app and Set & Forget does not get a second one to drift from it.

Two reads, deliberately split the way `api/routers/ai.py` splits its own:

- `GET ""` is the measured evidence, the zones, the candidate the rules
  produce and the confluence score. No model is called and nothing is billed.
  Most of the time this is the whole answer, and a page that could only show it
  by billing an API call would make every glance billable.
- `POST /evaluate` adds the configured model's review on top. **Billable**, and
  the response says so. The model is only asked when there is a candidate to
  judge — see `services/setforget/analysis.evaluate`.

`value_per_lot` is the one piece of money arithmetic that crosses to the
browser, and it crosses as a per-lot figure the browser multiplies. It comes
from `fees_sizing.pnl`, this app's one P&L function: the position box has to
re-price as the operator moves the lot selector, and the alternative — a second
P&L implementation in TypeScript — is exactly the drift golden rule 5 forbids.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.src.api.deps import engine as engine_dep
from backend.src.api.errors import Refusal
from backend.src.controllers import ai_controller as ai_ctl
from backend.src.controllers import engines_controller as engines_ctl
from backend.src.controllers import settings_controller as settings_ctl
from backend.src.controllers import setforget_controller as sf_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/trading/setforget", tags=["trading"])

# What a Set & Forget trade is tagged with, so these can be told apart from a
# plain manual order in the history. Sent by the browser with the order.
STRATEGY = "set_and_forget"
SOURCE_NAME = "Set & Forget"


class SetForgetSettings(BaseModel):
    """`lot_size` 0 means "size it from Risk per trade % and the stop"."""

    lot_size: Optional[float] = None


async def _balance(eng: Any) -> Optional[float]:
    """The account balance the risk-based lot is sized against.

    None rather than a default when the bridge cannot answer. A lot size
    computed against an invented balance would look exactly like a real one and
    would go to a broker.
    """
    try:
        account = await eng.get_mt5_account()
    except Exception as exc:                       # pragma: no cover - defensive
        log.warning("[setforget] could not read the account: %s", exc)
        return None
    balance = float((account or {}).get("balance") or 0.0)
    return balance if balance > 0 else None


def _sizing(candidate: Optional[dict], balance: Optional[float],
            risk_pct: float) -> dict:
    """What the lot selector needs: a suggestion, and the per-lot cash."""
    if not candidate:
        return {"suggested_lot": None, "risk_per_lot": None, "reward_per_lot": None}
    return {
        "suggested_lot": sf_ctl.lot_from_risk(
            candidate["entry"], candidate["stop_loss"], balance or 0.0, risk_pct),
        "risk_per_lot": sf_ctl.money_at_risk(candidate, 1.0),
        "reward_per_lot": sf_ctl.money_at_target(candidate, 1.0),
    }


def _context(settings: dict, balance: Optional[float], cfg: dict) -> dict:
    """The parts of the response that do not depend on there being a setup."""
    return {
        "lot_size": float(settings.get("setforget_lot_size") or 0.0),
        "risk_per_trade_pct": float(settings.get("risk_per_trade_pct") or 0.0),
        "balance": balance,
        "min_rr": sf_ctl.MIN_RR,
        "preferred_rr": sf_ctl.PREFERRED_RR,
        "strategy": STRATEGY,
        "source_name": SOURCE_NAME,
        # So the button can say WHERE an order would land before it is pressed
        # rather than after.
        "control_target": engines_ctl.control_target(),
        "ai_configured": ai_ctl.is_configured(cfg),
        # Named so the page can say which model is about to be billed.
        "ai_provider": cfg.get("ai_provider", ""),
        "ai_model": ai_ctl.active_model(cfg),
    }


@router.get("")
async def state(eng: Any = Depends(engine_dep)) -> dict:
    """The chart read, the zones, the candidate and the checklist.

    Nothing is billed here. `candidate` is null when the rules refuse, and
    `no_setup_reason` says which rule refused — "no setup" on a page the
    operator just opened is indistinguishable from a broken one, and "the
    Weekly is bullish and the Daily is bearish" is the method working.
    """
    try:
        evidence = await sf_ctl.read_chart(eng)
    except Exception as exc:
        log.warning("[setforget] could not read the chart: %s", exc)
        raise Refusal(f"Could not read the chart: {exc}") from exc

    candidate, why = sf_ctl.propose(evidence)
    settings = sf_ctl.get_risk_settings() or {}
    balance = await _balance(eng)
    cfg = settings_ctl.load_config()

    return {
        # Same key as the billable read carries, so the page has one
        # renderer and one "as of" line rather than two.
        "generated_at": time.time(),
        "price": evidence.get("price"),
        # Asked for, not restated. This route used to spell the series names
        # out here, and the two lists drifted: `trigger_candles` joined the
        # evidence, the billable read dropped it and this one -- polled every
        # sixty seconds -- shipped it to the browser.
        "evidence": sf_ctl.public_evidence(evidence),
        "candidate": candidate,
        "no_setup_reason": why,
        "confluence": sf_ctl.score(evidence, candidate),
        # The free read applies the rules too. `propose` builds the best
        # candidate the zones allow; whether it is TRADEABLE is a separate
        # question, and leaving it unanswered until someone pays for an AI
        # review would put a 1:0.05 setup on screen looking exactly as tidy as
        # a 1:3 one — with an Execute button that is not disabled.
        "invalidations": sf_ctl.invalidations(candidate),
        "ai": None,
        "billed": False,
        **_sizing(candidate, balance,
                  float(settings.get("risk_per_trade_pct") or 0.5)),
        **_context(settings, balance, cfg),
    }


@router.post("/evaluate")
async def evaluate(eng: Any = Depends(engine_dep)) -> dict:
    """Have the configured model judge the candidate. **Billable.**

    The response is the same shape as `GET ""` with `ai` filled in, so the page
    has one renderer rather than two that can disagree about how a setup looks.

    The model's levels are re-validated before they reach this response; what
    fails the method's own rules is discarded and reported in
    `ai.levels_rejected` rather than shown as a trade.
    """
    cfg = settings_ctl.load_config()
    if not ai_ctl.is_configured(cfg):
        raise Refusal(
            "No AI provider is configured, so there is nothing to evaluate "
            "with. Set a provider and key in Settings > AI. The rules-based "
            "reading on this page works without one."
        )
    try:
        result = await sf_ctl.evaluate(eng, cfg)
    except Exception as exc:
        log.warning("[setforget] the evaluation failed: %s", exc)
        raise Refusal(f"The evaluation could not be completed: {exc}") from exc

    settings = sf_ctl.get_risk_settings() or {}
    balance = await _balance(eng)
    return {
        **result,
        **_sizing(result.get("candidate"), balance,
                  float(settings.get("risk_per_trade_pct") or 0.5)),
        **_context(settings, balance, cfg),
    }


@router.put("/settings")
async def save_settings(body: SetForgetSettings) -> dict:
    """The section's lot size. An ordinary risk setting, kept here because it
    means nothing away from this section."""
    if body.lot_size is None:
        raise Refusal("Nothing to save.")
    if body.lot_size < 0:
        raise Refusal("A lot size cannot be negative. Use 0 to size it from "
                      "the risk percentage.", status_code=400)

    sf_ctl.update_risk_settings({"setforget_lot_size": float(body.lot_size)})
    settings = sf_ctl.get_risk_settings() or {}
    return {"lot_size": float(settings.get("setforget_lot_size") or 0.0)}
