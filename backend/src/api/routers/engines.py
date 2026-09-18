"""Signal Generator tab — the engines that produce signals of their own.

Three engines behind one tab: Breakout, Bounce and Reversal. They are
structurally near-identical and **deliberately not collapsed** — the frontend
conventions record that explicitly, because "they look alike and behave
differently". This router keeps them separate for the same reason: one generic
`/engines/{name}/do` would invite the panels to merge.

**Starting an engine is not placing an order**, but it is the switch that lets
one be placed without anybody watching, so every control here is a POST and the
tab states what is running before it offers to change it.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from backend.src.api.errors import Refusal
from backend.src.controllers import engines_controller as engines_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/engines", tags=["engines"])

# Named for what the operator calls them. `test_panel.py` was the Bounce engine
# — the standing example in this repo of what naming a surface after its
# service costs.
ENGINE_LABELS = {
    "breakout": "Breakout",
    "bounce": "Bounce",
    "reversal": "Reversal",
}


class EngineAction(BaseModel):
    engine: str
    running: bool


class TunableUpdate(BaseModel):
    model_config = {"extra": "allow"}


class AiSettings(BaseModel):
    settings: dict


def _engine_or_refuse(name: str):
    if name not in ENGINE_LABELS:
        raise Refusal(f"Unknown engine {name!r}. "
                      f"Known: {', '.join(ENGINE_LABELS)}.", status_code=400)
    return engines_ctl.get_engine(name)


@router.get("/state")
async def state() -> dict:
    """What is running, and the settings the panels edit."""
    running = engines_ctl.engines_running()
    return {
        "engines": [
            {"id": key, "label": label,
             "running": bool(running.get(key, False)),
             # An engine slot with no service is a real state — Bounce is one
             # on an install that never enabled it — and reads differently from
             # "built but stopped".
             "built": engines_ctl.get_engine(key) is not None}
            for key, label in ENGINE_LABELS.items()
        ],
        "settings": await engines_ctl.get_risk_settings_async(),
        "pro_model": engines_ctl.pro_model_status(),
    }


@router.get("/reversal/report")
async def reversal_report() -> dict:
    """The Reversal engine's own measurements. Reads history, places nothing."""
    return {
        "realised": await engines_ctl.reversal_realised_pnl(),
        "shadow": engines_ctl.reversal_shadow_report(),
    }


@router.post("/running")
async def set_running(body: EngineAction) -> dict:
    """Start or stop ONE engine, by name.

    Named rather than bulk. `start_stopped_engines()` exists for the app's own
    startup and deliberately skips Bounce; a UI button that called it would
    start engines the operator did not ask for.
    """
    engine = _engine_or_refuse(body.engine)
    if engine is None:
        raise Refusal(
            f"The {ENGINE_LABELS[body.engine]} engine is not built on this "
            "install, so there is nothing to start.",
        )
    if body.running:
        engine.start()
    else:
        engine.stop()
    return {"engine": body.engine,
            "running": bool(engines_ctl.engines_running().get(body.engine, False))}


@router.put("/settings")
async def update_settings(body: TunableUpdate) -> dict:
    """Write engine tunables. Partial, one key at a time, as the switches save."""
    engines_ctl.update_risk_settings(dict(body.model_dump()))
    return await engines_ctl.get_risk_settings_async()


@router.post("/reversal/fit")
async def fit_pro_model() -> dict:
    """Retrain the pro-signal model, in the background.

    **Never the blocking fit.** A five-second RandomForest train started from a
    UI handler freezes the event loop, the EA socket reader and the monitor
    loop together, and the EA reconnects after ten seconds of Python silence.
    `pro_model_fit_in_background` is the only version this layer may call, and
    `tests/reversal_engine/test_panel_fit_does_not_freeze_the_ui.py` asserts
    that no module under `backend/src/api/` names the blocking one.
    """
    engines_ctl.pro_model_fit_in_background(force=True)
    return {"started": True, "status": engines_ctl.pro_model_status()}


@router.post("/reversal/study")
async def reversal_study() -> dict:
    """Run the phase-1 research study and render it. Reads history, writes two
    measurement columns, places nothing."""
    return {"report": await engines_ctl.reversal_research_study()}


@router.post("/reversal/ai/recommend")
async def reversal_ai_recommend() -> dict:
    """Ask the configured model for capability settings. **Billable.** Writes
    nothing — the operator decides whether to apply it."""
    return {"billable": True, "recommendation": await engines_ctl.reversal_ai_recommend()}


@router.post("/reversal/ai/apply")
async def reversal_ai_apply(body: AiSettings) -> dict:
    """Write a recommendation the operator accepted.

    The controller re-sanitises what arrives rather than trusting it: this has
    been through a UI and back, and the allowlist is the only thing between a
    model's output and a live trading setting.
    """
    return {"applied": engines_ctl.reversal_ai_apply(body.settings)}
