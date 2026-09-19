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
from backend.src.controllers import breakout_controller as breakout_ctl
from backend.src.controllers import reversal_controller as reversal_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/engines", tags=["engines"])

# Named for what the operator calls them. `test_panel.py` was the Bounce engine
# — the standing example in this repo of what naming a surface after its
# service costs.
#
# The KEYS come from the engine registry rather than being restated, so this
# tab cannot disagree with the rest of the app about which engines exist. A
# name with no label here falls back to its id, which reads as an oversight
# rather than hiding the engine.
_LABELS = {"breakout": "Breakout", "reversal": "Reversal"}
# IMPLEMENTED_NAMES, not ENGINE_NAMES. The latter is the sync protocol's
# positional order and still carries Bounce's empty slot, because dropping the
# name would shift Reversal into its place on the wire. The SCREEN must not
# show it: its code was deleted on 2026-09-14, so a card with a Start button
# is a control that cannot work. Owner's request, 2026-09-19.
ENGINE_LABELS = {name: _LABELS.get(name, name)
                 for name in engines_ctl.IMPLEMENTED_NAMES}


# How much of the virtual ledger the panel gets. Bounded here rather than by
# the browser: it is a row per variant per signal, and both numbers grow.
HISTORY_LIMIT = 200


class EngineAction(BaseModel):
    engine: str
    running: bool


class TunableUpdate(BaseModel):
    model_config = {"extra": "allow"}


class AiSettings(BaseModel):
    settings: dict


class AiEval(BaseModel):
    engine: str
    # None means "invert what is current". See the handler.
    enabled: bool | None = None


def _label(name: str) -> str:
    """What to call an engine in a message.

    Falls back to the raw name, because a name can be addressable without
    being on this build's screen -- Bounce is, for a peer that still runs it --
    and a KeyError here would turn a plain refusal into a 500.
    """
    return ENGINE_LABELS.get(name, _LABELS.get(name, name))


def _known_or_refuse(name: str) -> None:
    """Reject a name this build has no engine for, before anything is sent.

    Separate from "not built on this install", which is a different answer and
    only applies when the command was going to be applied here at all — in
    Remote mode the engine that matters is the peer's.
    """
    # ENGINE_NAMES, not ENGINE_LABELS. The labels are what this build SHOWS,
    # and Bounce left that list on 2026-09-19; the names are what the sync
    # protocol carries, and a paired node on an older build may still have
    # Bounce. Validating against the screen would make an engine the peer
    # genuinely runs unaddressable from here.
    if name not in engines_ctl.ENGINE_NAMES:
        raise Refusal(f"Unknown engine {name!r}. "
                      f"Known: {', '.join(engines_ctl.ENGINE_NAMES)}.",
                      status_code=400)


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
        # The settings the engines are OBEYING, which in Remote mode are the
        # peer's. Showing this node's row while the VPS trades would describe a
        # machine nobody is watching.
        "settings": engines_ctl.effective_settings(
            await engines_ctl.get_risk_settings_async()),
        "pro_model": reversal_ctl.pro_model_status(),
        # Which node a control will reach. Three states, not two:
        # "centralized" is the VPS-trades-but-generation-moved-here case, where
        # the header says REMOTE and these engines are still the live ones.
        "control_target": engines_ctl.control_target(),
        "ai_eval_keys": engines_ctl.AI_EVAL_KEYS,
    }


async def _guarded(read, fallback):
    """One panel read, or the fallback.

    The Breakout engine keeps its own database and a fresh install has none,
    so a missing table must not take the whole panel down with it -- the same
    convention `services/dpm/performance.py` uses. A missing NUMBER falls back
    to None rather than 0: a 0.0 drawdown reads as an engine that never lost,
    and a fresh install is not that.
    """
    try:
        return await read()
    except Exception as exc:
        log.debug("[engines] breakout panel read unavailable: %s", exc)
        return fallback


@router.get("/breakout/report")
async def breakout_report() -> dict:
    """Everything the Breakout panel shows, in one read. Places nothing.

    One endpoint rather than nine: the panel reads them together on every
    refresh, and nine round-trips can disagree about which moment they
    describe.
    """
    def thresholds() -> dict:
        try:
            return breakout_ctl.breakout_ml_thresholds()
        except Exception:
            return {}

    return {
        "stats": await _guarded(breakout_ctl.breakout_stats, {}),
        "edge": await _guarded(breakout_ctl.breakout_edge_stats, {}),
        "virtual_balance": await _guarded(breakout_ctl.breakout_virtual_balance, None),
        "max_drawdown": await _guarded(breakout_ctl.breakout_max_drawdown, None),
        "ml": {
            "summary": await _guarded(breakout_ctl.breakout_ml_summary, {}),
            "metrics": await _guarded(breakout_ctl.breakout_ml_metrics, {}),
            "thresholds": thresholds(),
        },
        "by_session": await _guarded(breakout_ctl.breakout_perf_by_session, []),
        "by_adx": await _guarded(breakout_ctl.breakout_perf_by_adx_band, []),
        "by_type": await _guarded(breakout_ctl.breakout_perf_by_type, []),
        "by_bias": await _guarded(breakout_ctl.breakout_perf_by_bias, []),
    }


@router.get("/reversal/report")
async def reversal_report() -> dict:
    """The Reversal engine's own measurements. Reads history, places nothing.

    `history` is the virtual trade ledger: one row per variant decision, with
    what that decision would have earned. The owner asked for it on 2026-09-19
    -- the aggregates above say which variant is ahead and cannot say what
    either of them did last Tuesday.
    """
    return {
        "realised": await reversal_ctl.reversal_realised_pnl(),
        "shadow": reversal_ctl.reversal_shadow_report(),
        "history": reversal_ctl.reversal_shadow_history(HISTORY_LIMIT),
    }


@router.post("/running")
async def set_running(body: EngineAction) -> dict:
    """Start or stop ONE engine, by name.

    Named rather than bulk. The bulk start (`services/engines/registry.py`)
    exists for the app's own startup and the Local/Remote handover, and
    deliberately skips Bounce; a UI button wired to it would start engines the
    operator did not ask for.

    **It may not land on this machine.** When the VPS is the active trader,
    this node's sub-engines are stood down, so starting one here would start
    something that generates nothing while the screen said it was running. The
    controller routes the command to whichever node is actually trading; the
    response says which, so the panel can too.
    """
    _known_or_refuse(body.engine)

    # "Not built here" is only a reason to refuse when the command was going to
    # be applied here. In Remote mode the engine that matters is the peer's.
    if (engines_ctl.control_target() != "remote"
            and engines_ctl.get_engine(body.engine) is None):
        raise Refusal(
            f"The {_label(body.engine)} engine is not built on this "
            "install, so there is nothing to start.",
        )
    try:
        return await engines_ctl.set_engine_running(body.engine, body.running)
    except engines_ctl.RemoteControlFailed as exc:
        raise Refusal(str(exc)) from exc


@router.put("/settings")
async def update_settings(body: TunableUpdate) -> dict:
    """Write engine tunables. Partial, one key at a time, as the switches save.

    **Local only, and that is a limit rather than a choice.** The sync protocol
    carries exactly one risk setting between nodes — the AI-evaluation flag,
    which has its own endpoint below. Everything else has no remote route, so
    in Remote mode these write a row the trading node will not read. The
    dashboard says so rather than pretending otherwise; `control_target` on
    `/state` is what it says it with.
    """
    engines_ctl.update_risk_settings(dict(body.model_dump()))
    return engines_ctl.effective_settings(
        await engines_ctl.get_risk_settings_async())


@router.post("/ai-eval")
async def set_ai_eval(body: AiEval) -> dict:
    """Turn AI review of an engine's signals on or off, on the trading node.

    `enabled` omitted means "invert what is current", which is what a toggle
    wants — and the only form where reading "current" from the right place
    matters. Taking it from this node's row while writing to the peer makes the
    toggle one-directional: it recomputes the same current on every click and
    re-sends the same target state for ever. That was confirmed live, with
    Bounce stuck OFF and Breakout stuck ON.
    """
    _known_or_refuse(body.engine)
    try:
        return await engines_ctl.set_ai_eval(body.engine, body.enabled)
    except engines_ctl.RemoteControlFailed as exc:
        raise Refusal(str(exc)) from exc


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
    reversal_ctl.pro_model_fit_in_background(force=True)
    return {"started": True, "status": reversal_ctl.pro_model_status()}


@router.post("/reversal/study")
async def reversal_study() -> dict:
    """Run the phase-1 research study and render it. Reads history, writes two
    measurement columns, places nothing."""
    return {"report": await reversal_ctl.reversal_research_study()}


@router.post("/reversal/reset-stats")
async def reversal_reset_stats() -> dict:
    """Start the Reversal panel's numbers again from now.

    **Reporting only.** No signal row, stored feature vector, reconstructed
    excursion or attribution history is removed -- the engine's memory is
    untouched and only the panel's counters restart. Saying which is the whole
    point: "reset" next to a machine-learning engine reads as "forget what you
    learned", and an operator who believed that would avoid pressing it.
    """
    return {"since": await reversal_ctl.reversal_reset_stats()}


@router.post("/reversal/ai/recommend")
async def reversal_ai_recommend() -> dict:
    """Ask the configured model for capability settings. **Billable.** Writes
    nothing — the operator decides whether to apply it."""
    return {"billable": True, "recommendation": await reversal_ctl.reversal_ai_recommend()}


@router.post("/reversal/ai/apply")
async def reversal_ai_apply(body: AiSettings) -> dict:
    """Write a recommendation the operator accepted.

    The controller re-sanitises what arrives rather than trusting it: this has
    been through a UI and back, and the allowlist is the only thing between a
    model's output and a live trading setting.
    """
    return {"applied": reversal_ctl.reversal_ai_apply(body.settings)}
