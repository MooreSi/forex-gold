"""AI Analysis tab — ask the configured model what the evidence says.

**Every endpoint that calls a model is billable, and both say so.** The old page
put the warning in a tooltip; here it is in the response, because the browser
renders what the backend reports and a cost that only exists in a comment gets
dropped in a redesign.

Nothing here decides anything. The model reads history and returns prose; what
the operator does with it is a separate action on a separate tab.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Query
from pydantic import BaseModel

from backend.src.api.errors import Refusal
from backend.src.controllers import ai_analysis_controller as ai_analysis_ctl
from backend.src.controllers import ai_controller as ai_ctl
from backend.src.controllers import dpm_controller as dpm_ctl
from backend.src.controllers import settings_controller as settings_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["ai"])

DAYS = Query(30, ge=1, le=365)

# What can be analysed, and which gatherer answers for it. Data rather than a
# chain of ifs, so adding a subject is one line and the endpoint cannot grow a
# branch.
SUBJECTS = {
    "channels": ("Telegram channels", "gather_channel_data"),
    "strategies": ("Fixed strategies vs DPM", "gather_strategy_dpm_data"),
    "generator": ("The internal signal generator", "gather_signal_generator_data"),
}


class AnalysisRequest(BaseModel):
    subject: str
    days: int = 30


@router.get("/subjects")
async def subjects() -> dict:
    """What can be analysed, and whether a model is configured to do it."""
    cfg = settings_ctl.load_config()
    return {
        "subjects": [{"id": k, "label": v[0]} for k, v in SUBJECTS.items()],
        "configured": ai_ctl.is_configured(cfg),
        # Named so the page can say WHICH model is about to be billed.
        "provider": cfg.get("ai_provider", ""),
        "model": cfg.get("claude_model", ""),
    }


@router.get("/evidence")
async def evidence(subject: str = Query(...), days: int = DAYS) -> dict:
    """The measured evidence, with no model involved and nothing billed.

    Its own endpoint on purpose: the numbers are the answer most of the time,
    and reading them should not cost anything. A page that could only show them
    by asking a model would make every glance billable.
    """
    if subject not in SUBJECTS:
        raise Refusal(f"Unknown subject {subject!r}. "
                      f"Known: {', '.join(SUBJECTS)}.", status_code=400)
    gather = getattr(ai_analysis_ctl, SUBJECTS[subject][1])
    path = settings_ctl.get_config("db_path") or ""
    if not path:
        raise Refusal("No database path is configured for this environment.")
    return {"subject": subject, "days": days, "billable": False,
            "evidence": gather(path, days)}


@router.get("/dpm")
async def dpm() -> dict:
    """The DPM calibration tables. Local reads; nothing billed."""
    return {
        "performance": await dpm_ctl.get_perf_rows(),
        "calibration": await dpm_ctl.get_calibration_rows(),
        "runs": await dpm_ctl.get_calibration_runs(),
    }


@router.post("/analyse")
async def analyse(body: AnalysisRequest) -> dict:
    """Send the evidence to the configured model. **Billable.**

    Refuses rather than silently doing nothing when no model is configured: an
    Analyse button that returns an empty answer looks like a model with no
    opinion, which is a different and much more interesting result.
    """
    if body.subject not in SUBJECTS:
        raise Refusal(f"Unknown subject {body.subject!r}.", status_code=400)

    cfg = settings_ctl.load_config()
    if not ai_ctl.is_configured(cfg):
        raise Refusal(
            "No AI provider is configured. Add a provider and an API key under "
            "Settings → AI before asking for an analysis.",
        )

    gather = getattr(ai_analysis_ctl, SUBJECTS[body.subject][1])
    path = settings_ctl.get_config("db_path") or ""
    evidence_rows = gather(path, body.days)
    # The prompt for THIS subject. Until 2026-09-19 every subject was sent the
    # signal-generator one, so asking about Telegram channels handed the model
    # channel rows and told it they were engines.
    prompt = ai_analysis_ctl.system_prompt_for(body.subject)
    answer = await ai_ctl.complete(cfg, prompt, str(evidence_rows), 4000)
    return {"subject": body.subject, "days": body.days,
            "billable": True, "answer": answer}
