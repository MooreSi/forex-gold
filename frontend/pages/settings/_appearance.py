"""Theme selection and the licence registration card."""
from nicegui import ui


def _render_theme():
    """Settings → Theme tab — pick Light or Dark for the whole app.

    See core_ui_theme.py's module docstring for how the neutral-class CSS
    override mechanism works (and why text-white is excluded on buttons).
    """
    from frontend import theme as theme_mod

    with ui.column().classes("w-full max-w-2xl gap-3"):
        ui.label("Color Theme").classes("text-base font-bold text-yellow-300")
        ui.label(
            "Applies to the whole app immediately — no restart needed. "
            "Status colors (profit/loss/warnings) never change."
        ).classes("text-xs text-gray-400 mb-2")

        current = {"value": theme_mod.get_theme()}
        cards: dict[str, object] = {}

        def _select(name: str) -> None:
            theme_mod.set_theme(name)
            current["value"] = name
            for key, card in cards.items():
                card.classes(
                    remove="border-blue-500" if key != name else "",
                    add="border-blue-500" if key == name else "border-gray-700",
                )
            ui.run_javascript(
                f'document.documentElement.setAttribute("data-fx-theme","{name}")'
            )
            ui.notify(f"Theme set to {theme_mod.THEME_LABELS[name]}", type="positive")

        with ui.row().classes("gap-3 flex-wrap"):
            for name in theme_mod.THEMES:
                bg, border, text = theme_mod.THEME_SWATCHES[name]
                is_active = name == current["value"]
                card = ui.card().classes(
                    "cursor-pointer p-3 w-48 border-2 "
                    + ("border-blue-500" if is_active else "border-gray-700")
                ).style(f"background:{bg};")
                cards[name] = card
                with card:
                    card.on("click", lambda _e, n=name: _select(n))
                    ui.label(theme_mod.THEME_LABELS[name]).style(
                        f"color:{text};font-weight:600;font-size:13px;"
                    )
                    ui.label(theme_mod.THEME_DESCRIPTIONS[name]).style(
                        f"color:{text};font-size:11px;opacity:0.8;"
                    )
                    with ui.row().classes("gap-1 mt-2"):
                        for swatch_color in (bg, border, text):
                            ui.element("div").style(
                                f"width:16px;height:16px;border-radius:3px;"
                                f"background:{swatch_color};border:1px solid #00000040;"
                            )


def _render_registration():
    """Settings → Registration tab — shows stored licence / account details."""
    from backend.src.config.licence import store as _store
    from backend.src.config.licence.fingerprint import get_fingerprint

    data       = _store.load()
    email      = data.get("email", "") if data else ""
    raw_key    = data.get("licence_key", "") if data else ""
    machine_id = get_fingerprint()

    # Masked key: show first group then stars for the rest
    if raw_key:
        first_group = raw_key.split("-")[0] if "-" in raw_key else raw_key[:8]
        masked_key  = first_group + " - **** - **** - **** - **** - **** - **** - ****"
    else:
        masked_key = "—"

    from datetime import date as _date

    def _reg_days_remaining(expiry_date: str) -> tuple[str, str]:
        if not expiry_date or expiry_date == "perpetual":
            return "Unlimited", "text-green-400"
        try:
            exp   = _date.fromisoformat(expiry_date)
            delta = (exp - _date.today()).days
            if delta < 0:
                return "Expired", "text-red-400"
            if delta == 0:
                return "Expires today", "text-orange-400"
            colour = "text-orange-400" if delta <= 30 else "text-green-400"
            return f"{delta} days", colour
        except Exception:
            return "—", "text-gray-400"

    expiry_date  = data.get("expiry_date", "perpetual") if data else "perpetual"
    licence_type = (data.get("licence_type") or (
        "Perpetual" if expiry_date == "perpetual" else "Fixed Term"
    )) if data else "—"
    days_txt, days_cls = _reg_days_remaining(expiry_date)

    with ui.card().classes("w-full max-w-xl bg-gray-800 p-6 rounded-lg"):
        ui.label("Registration").classes("text-lg font-bold text-yellow-300 mb-4")

        with ui.grid(columns=2).classes("gap-x-8 gap-y-3 text-sm w-full"):
            ui.label("Email").classes("text-gray-400 self-center")
            ui.label(email or "—").classes("text-white font-mono")

            ui.label("Registration Key").classes("text-gray-400 self-center")
            ui.label(masked_key).classes("text-gray-300 font-mono text-xs break-all")

            ui.label("Machine ID").classes("text-gray-400 self-center")
            ui.label(machine_id).classes("text-gray-300 font-mono text-xs break-all")

            ui.label("Licence Type").classes("text-gray-400 self-center")
            ui.label(licence_type).classes("text-gray-300")

            ui.label("Remaining Days").classes("text-gray-400 self-center")
            ui.label(days_txt).classes(days_cls)
