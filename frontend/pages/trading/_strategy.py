"""Strategy configuration: channel overrides, parameters, global settings
and EA templates."""
import json
from nicegui import ui
from backend.src.controllers import trading_controller as trading_ctl
from backend.src.controllers.trading_controller import (
    STRATEGY_NAMES,
    STRATEGY_SCALE_OUT,
)
from frontend.pages.settings import render_risk_card

# The strategy id short-aliases (_SO, _BE, _TS ...) and strategy_controller
# were imported solely for the comparison tables and went with them.
from ._ea_templates import _render_ea_templates_card
from backend.src.controllers.trading_controller import (
    STRATEGY_ORB_FIXED,
)
from ._strategy_cards import (
    _render_channel_strategy_card,
    _render_global_parameters_card,
    _render_strategy_params_card,
)


# The Quick comparison table and all of its cell data were removed
# 2026-09-02 on the owner's instruction. It only READ strategy
# definitions and drew them side by side -- nothing selected a strategy
# through it. The cell helpers, _COMPARE_ROWS and the two group lists went
# with it; nothing else referenced them.


def _render_strategy(engine):
    outer = ui.column().classes("w-full gap-4")

    def _refresh():
        outer.clear()
        with outer:
            _draw()

    def _draw():  # noqa: C901  (complex but linear)
        rs            = trading_ctl.get_risk_settings()
        custom_strats = trading_ctl.get_custom_strategies()
        _hidden       = _get_hidden_strategies()
        custom_strats = [cs for cs in custom_strats if cs["id"] not in _hidden]
        custom_ids    = {cs["id"] for cs in custom_strats}

        # Resolve display ID (custom strategies have an extra display key)
        display_id = rs.get("display_strategy_id", "") or rs.get("trade_strategy", STRATEGY_SCALE_OUT)
        if display_id.startswith("custom_") and display_id not in custom_ids:
            display_id = rs.get("trade_strategy", STRATEGY_SCALE_OUT)
        if display_id in _hidden:
            display_id = STRATEGY_SCALE_OUT

        all_names = {
            k: v for k, v in STRATEGY_NAMES.items()
            # ORB/IVB is a time-of-day breakout engine, not a per-channel
            # strategy -- it has no signal/channel to attach to.
            if k not in _hidden and k != STRATEGY_ORB_FIXED
        }
        all_names.update({cs["id"]: cs["name"] for cs in custom_strats})

        # ── Top row: Strategy Parameters (half) + Channel Strategy (half) ────
        with ui.row().classes("w-full gap-4 flex-wrap items-stretch"):

          # ── Strategy Parameters card ─────────────────────────────────────
          with ui.card().classes("flex-1 min-w-72 bg-gray-800 p-2 rounded-lg"):
            _render_strategy_params_card()

          # ── Channel Strategy card ─────────────────────────────────────────
          with ui.card().classes("flex-1 min-w-72 bg-gray-800 p-2 rounded-lg"):
            _render_channel_strategy_card(engine, all_names, rs)

        # ── Global Parameters card (full width) ────────────────────────────────
        with ui.card().classes("w-full bg-gray-800 p-2 rounded-lg"):
            _render_global_parameters_card(rs)

        # ── EA Templates card (full width) ────────────────────────────────────
        with ui.card().classes("w-full bg-gray-800 p-2 rounded-lg"):
            _render_ea_templates_card()

        # ── Risk Settings (full width, own internal 3+2 sub-card layout) ────────
        # Internal Engine Exposure and DPM (formerly on an "Active Strategy" card
        # here) moved into Risk Settings itself (2026-08-01) — strategy is
        # selected per-channel in Channel Strategy above, so a separate "Active
        # Strategy" card had no strategy-selection content of its own left to
        # justify existing as a distinct tab/card. render_risk_card lays out its
        # own five sub-cards (3 top row + 2 bottom row); no outer card here.
        render_risk_card("w-full")


    _refresh()


def _get_hidden_strategies() -> set:
    raw = trading_ctl.get_app_config("hidden_strategies") or "[]"
    try:
        return set(json.loads(raw))
    except Exception:
        return set()
def _hide_builtin_strategy(sid: str) -> None:
    hidden = _get_hidden_strategies()
    hidden.add(sid)
    trading_ctl.set_app_config("hidden_strategies", json.dumps(sorted(hidden)))
