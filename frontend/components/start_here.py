"""First-run "Start Here" checklist — the app's answer to "what do I do?".

Pure view layer. The caller (the app shell) supplies a status dict of
booleans it already computes; this module turns it into a live checklist
with a "Fix this →" jump per unfinished row. It reads status and
navigates — it never acts: no engine, no services, no order path.

Shown on first boot until dismissed (``setup_seen`` in
``app.storage.user``) and reachable again from Help.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Mapping, Optional

from nicegui import app, ui

log = logging.getLogger(__name__)

# key, label, hint, optional, fix_tab, fix_section
# fix_tab "Settings" jumps to a Settings sub-tab (fix_section names it);
# any other fix_tab is a top-level tab; None means there is nothing to fix
# from inside the app (e.g. the Demo/Live switch sits in the tab bar).
_STEPS: list[tuple[str, str, str, bool, Optional[str], Optional[str]]] = [
    ("licence", "Licence active",
     "Your licence unlocked this app when it started. If it ever expires, "
     "Settings → Registration is where a new key goes.",
     False, "Settings", "Registration"),
    ("mt5", "MetaTrader 5 connected",
     "The app trades through MetaTrader 5. Start MT5, then start the bridge "
     "under Settings → MT5 / Bridge.",
     False, "Settings", "MT5 / Bridge"),
    ("algo", "Algo Trading enabled in MT5",
     "MT5 has its own master switch: the AutoTrading (robot) button in the "
     "MT5 toolbar must be ON or no order can be placed.",
     False, "Settings", "MT5 / Bridge"),
    ("risk", "Risk per trade set",
     "How much of the balance one trade may risk. Set it under "
     "Trading → Global Parameters before anything executes.",
     False, "Trading", None),
    ("telegram", "Telegram signal reader connected (optional)",
     "Optional: signals can arrive from Telegram channels. The app works "
     "without it — you can build signals yourself in Trading.",
     True, "Settings", "Telegram Alerts"),
    ("demo", "You are in Demo mode",
     "Demo means no real money moves. Practise here; the Demo/Live switch "
     "is at the top-left of the tab bar.",
     False, None, None),
]


def checklist_rows(status: Mapping[str, object]) -> list[dict]:
    """Merge the fixed step definitions with the caller's live status."""
    key_to_status = {
        "licence": "licence",
        "mt5": "mt5_connected",
        "algo": "algo_enabled",
        "risk": "risk_set",
        "telegram": "telegram_connected",
        "demo": "demo_mode",
    }
    return [
        {
            "key": key,
            "label": label,
            "hint": hint,
            "optional": optional,
            "fix_tab": fix_tab,
            "fix_section": fix_section,
            "done": bool(status.get(key_to_status[key])),
        }
        for key, label, hint, optional, fix_tab, fix_section in _STEPS
    ]


def should_show(storage: Mapping[str, object]) -> bool:
    """Show until the user dismisses it (setup_seen)."""
    return not storage.get("setup_seen")


def render(
    status: Mapping[str, object],
    on_fix: Callable[[str, Optional[str]], None],
    on_dismiss: Callable[[], None],
) -> None:
    """Render the checklist into the current container.

    ``on_fix(tab, section)`` navigates; ``on_dismiss()`` records
    ``setup_seen`` and closes whatever holds this panel.
    """
    with ui.column().classes("w-full gap-3"):
        ui.label("Start Here").classes("text-xl font-bold text-yellow-400")
        ui.label(
            "This checklist shows whether the app is ready to use. "
            "Green rows are done; red rows tell you what to do next."
        ).classes("text-xs text-gray-400 leading-relaxed")

        for row in checklist_rows(status):
            with ui.row().classes(
                "w-full items-start gap-3 bg-gray-800 rounded-lg p-3"
            ):
                if row["done"]:
                    ui.icon("check_circle").classes("text-green-400 text-xl shrink-0")
                elif row["optional"]:
                    ui.icon("radio_button_unchecked").classes("text-gray-500 text-xl shrink-0")
                else:
                    ui.icon("cancel").classes("text-red-400 text-xl shrink-0")
                with ui.column().classes("gap-1 flex-1"):
                    ui.label(row["label"]).classes(
                        "text-sm font-semibold "
                        + ("text-gray-200" if row["done"] else "text-gray-100")
                    )
                    ui.label(row["hint"]).classes("text-xs text-gray-400 leading-relaxed")
                if not row["done"] and row["fix_tab"] is not None:
                    tab, section = row["fix_tab"], row["fix_section"]
                    ui.button(
                        "Fix this →",
                        on_click=lambda _=None, t=tab, s=section: on_fix(t, s),
                    ).classes("bg-yellow-700 text-white text-xs px-3 py-1 shrink-0")

        with ui.row().classes("w-full justify-end gap-2 mt-2"):
            ui.button("Don't show this automatically", on_click=on_dismiss).classes(
                "bg-gray-700 text-gray-300 text-xs px-3 py-1"
            ).tooltip("You can reopen it any time from the Help (?) button")


async def gather_status(engine: Any, tg_reader: Any, demo_mode: bool) -> dict:
    """Best-effort reads of status the app already computes.

    Reads through the engine's health surface and controllers only (the
    caller supplies demo_mode — config stays the shell's import); an
    unreachable source shows as not-done with its fix hint, never an error.
    """
    from backend.src.controllers import settings_controller as _settings_ctl
    from backend.src.controllers import telegram_controller as _tg_ctl

    status = {
        # guard.enforce() gates boot — a running app has an active licence
        "licence": True,
        "mt5_connected": False,
        "algo_enabled": False,
        "risk_set": False,
        "telegram_connected": False,
        "demo_mode": demo_mode,
    }
    try:
        health = await engine.get_bridge_health()
        status["mt5_connected"] = bool(health.get("connected"))
        status["algo_enabled"] = (
            status["mt5_connected"] and health.get("trade_allowed") is not False
        )
    except Exception as exc:
        log.debug("start-here: bridge health unavailable: %s", exc)
    try:
        rs = _settings_ctl.get_risk_settings()
        status["risk_set"] = float(rs.get("risk_per_trade_pct") or 0) > 0
    except Exception as exc:
        log.debug("start-here: risk settings unavailable: %s", exc)
    try:
        tg = await _tg_ctl.get_reader_status(tg_reader)
        status["telegram_connected"] = bool(
            tg.get("connected") or tg.get("authenticated")
        )
    except Exception as exc:
        log.debug("start-here: telegram status unavailable: %s", exc)
    return status


def attach(
    tabs: Any,
    tab_by_name: Mapping[str, Any],
    get_engine: Callable[[], Any],
    get_tg_reader: Callable[[], Any],
    is_demo: Callable[[], bool],
) -> Callable[[], Any]:
    """Build the Start Here dialog into the current page and wire it up.

    Returns an async ``open_start_here()`` the shell (and Help) can call.
    Dismissing sets ``app.storage.user["setup_seen"]``.
    """
    with ui.dialog() as dialog, ui.card().classes(
        "bg-gray-900 p-5 rounded-lg w-full"
    ).style("max-width:640px"):
        body = ui.column().classes("w-full")

    def _fix(tab_name: str, _section: Optional[str]) -> None:
        dialog.close()
        target = tab_by_name.get(tab_name)
        if target is not None:
            tabs.set_value(target)

    def _dismiss() -> None:
        app.storage.user["setup_seen"] = True
        dialog.close()

    async def open_start_here() -> None:
        status = await gather_status(get_engine(), get_tg_reader(), is_demo())
        body.clear()
        with body:
            render(status, on_fix=_fix, on_dismiss=_dismiss)
        dialog.open()

    return open_start_here
