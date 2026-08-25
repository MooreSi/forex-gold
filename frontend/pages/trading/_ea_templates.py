"""EA trade templates — the per-channel template library.

A template fully replaces strategy dispatch for its channel by design,
so it wins over format-triggered defaults too.
"""
from typing import Optional
from nicegui import ui


def _render_ea_templates_card() -> None:
    """
    EA Templates: complete, self-contained, EA-managed trade-management
    definitions (Grid vs Single, TP/SL visibility, trailing method,
    breakeven rule, cancel-pending-siblings) -- a channel can be assigned a
    saved template in the Channel Strategy card below in place of a
    built-in strategy. Unlike Strategy Parameters above (which only
    retunes existing Python-managed strategies), a template fully
    replaces strategy dispatch and the EA manages the trade end-to-end --
    every field here is sent fresh on each open, so changing a template's
    values never needs an EA recompile. See core_ea_templates.py's module
    docstring. Harvest moved to Global Parameters (below) 2026-07-24 --
    it now applies account-wide to every open position regardless of how
    it was opened, not just this template's own trades. Anchor TP (added
    2026-07-24): a per-TP pips/pct ladder -- pips fill any level the raw
    signal didn't supply, pct always wins over the signal (which never
    states a close percentage) -- see core_open_trade.py's EA-handoff block.
    """
    from backend.src.services.broker import ea_templates as et

    with ui.row().classes("items-center gap-2 mb-2"):
        ui.label("EA Templates").classes("text-base font-bold text-yellow-300")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            "Complete EA-managed trade definitions -- assign one to a channel "
            "(Channel Strategy, right) in place of a built-in strategy. The EA "
            "reads every field fresh on each open, no recompile needed."
        )

    state = {"name": None}  # None = new/unsaved template
    fields: dict[str, object] = {}

    body = ui.column().classes("w-full gap-2")

    def _load(name: Optional[str]) -> None:
        state["name"] = name
        _draw_body()

    def _current_values() -> dict:
        out = {}
        for k, f in fields.items():
            v = f.value
            if isinstance(v, dict):  # NiceGUI dict-options select
                v = v.get("value")
            out[k] = v
        return out

    def _copy_ladder(src_prefix: str, dst_prefix: str) -> None:
        """Copy one TP ladder onto the other (the panel's Copy to Pending /
        Copy to Anchor buttons). Anchor and pending ladders are separate by
        design -- the copier ships wider pending targets -- but starting one
        from the other and then tweaking is the common case."""
        for n in range(1, et.MAX_TP_LEVELS + 1):
            for suffix in ("pips", "pct"):
                src = fields.get(f"{src_prefix}{n}_{suffix}")
                dst = fields.get(f"{dst_prefix}{n}_{suffix}")
                if src is not None and dst is not None:
                    dst.value = src.value

    def _send_to_ea() -> None:
        """Push the form's current values to the live EA immediately.

        Templates are normally sent with each open_trade, so a saved change
        reaches the EA on the NEXT signal. This is the panel's green Send
        button: it pushes now, so an adjustment can be made mid-session
        without waiting for a new trade. No-ops harmlessly when the EA is
        not connected."""
        from backend.src.services.broker import ea_bridge as _eab
        ea = _eab.get_instance()
        if ea is None or not ea.is_ea_healthy():
            ui.notify("EA not connected — values saved, will apply on next signal",
                      type="warning")
            return
        name = (state["name"] or "").strip()
        if not name:
            ui.notify("Save the template first, then Send", type="warning")
            return
        try:
            from backend.src.db.database import _schedule_coro
            _schedule_coro(ea.push_template(name, _current_values()))
            ui.notify(f"Sent '{name}' to the EA", type="positive")
        except Exception as exc:
            ui.notify(f"Send failed: {exc}", type="negative")

    def _export_templates() -> None:
        """Save every saved template to a shareable file.

        Exports what is in the Load drop-down (i.e. the saved templates),
        NOT the unsaved values currently in the form -- an edit has to be
        saved before it can be exported, same as it has to be saved before
        a channel can use it."""
        try:
            saved = et.list_ea_templates()
            if not saved:
                ui.notify("No saved templates to export", type="warning")
                return
            ui.download.content(
                et.export_templates(), et.export_filename(),
                media_type="application/json",
            )
            ui.notify(f"Exporting {len(saved)} template(s)", type="positive")
        except Exception as exc:
            ui.notify(f"Export failed: {exc}", type="negative")

    def _build_import_dialog():
        """Build the Import popup once, as a sibling of `body`.

        Deliberately NOT built inside `body`: the import handler ends with
        a _draw_body(), which clears `body` -- a dialog living in there
        would be destroyed out from under its own running handler."""
        with ui.dialog() as dlg, ui.card().classes(
            "bg-gray-900 border border-gray-700 p-4 gap-2 min-w-96"
        ):
            ui.label("Import Templates").classes(
                "text-sm font-bold text-yellow-300")
            ui.label(
                f"Choose a template file ({et.EXPORT_EXTENSION} or .json) exported "
                "from another install. Its templates are added to your Load list."
            ).classes("text-xs text-gray-400")
            overwrite = ui.checkbox("Overwrite templates with the same name") \
                .classes("text-xs").tooltip(
                    "Off: a template whose name you already use is skipped and "
                    "your own version is kept. On: the file's version replaces yours."
                )

            def _handle(e) -> None:
                try:
                    res = et.import_templates(
                        e.content.read(), overwrite=bool(overwrite.value))
                except Exception as exc:
                    ui.notify(f"Import failed: {exc}", type="negative")
                    return
                dlg.close()
                parts = []
                for label, key in (("added", "added"), ("replaced", "replaced"),
                                   ("skipped", "skipped")):
                    if res[key]:
                        parts.append(f"{len(res[key])} {label}")
                if not parts:
                    ui.notify("File contained no templates", type="warning")
                    return
                ui.notify(
                    f"Imported from {e.name}: " + ", ".join(parts),
                    type="positive" if (res["added"] or res["replaced"]) else "warning",
                )
                if res["skipped"]:
                    ui.notify(
                        "Kept your existing: " + ", ".join(res["skipped"])
                        + " — re-import with Overwrite ticked to replace them.",
                        type="info",
                    )
                _draw_body()

            uploader = ui.upload(
                on_upload=_handle, auto_upload=True, max_files=1,
                max_file_size=8 * 1024 * 1024, label="Select template file",
            ).classes("w-full").props('accept=".json,.eatpl" flat dense')
            with ui.row().classes("w-full justify-end"):
                ui.button("Cancel", on_click=dlg.close) \
                    .classes("text-xs").props("dense flat")
        return dlg, uploader

    _import_dialog, _import_uploader = _build_import_dialog()

    def _open_import_dialog() -> None:
        # Clear any previous run's file chip so a second import starts
        # from an empty picker rather than the last file's name.
        _import_uploader.reset()
        _import_dialog.open()

    def _draw_body() -> None:
        body.clear()
        live = (et.get_ea_template(state["name"]) if state["name"] else None) or dict(et.DEFAULTS)
        fields.clear()
        N = et.MAX_TP_LEVELS
        with body:
            with ui.row().classes("items-center gap-2 mb-2"):
                existing = et.list_ea_templates()
                load_opts = {"": "— New Template —"}
                load_opts.update({t["name"]: t["name"] for t in existing})
                ui.select(
                    load_opts, value=state["name"] or "", label="Load",
                ).classes("w-56").props("dense outlined").on_value_change(
                    lambda e: _load(e.value or None)
                ).tooltip(
                    "Load a saved template's values into the form for editing, "
                    "or leave on \"New Template\" to build one from scratch."
                )
                name_input = ui.input(
                    "Template name", value=state["name"] or "",
                ).classes("w-56").props("dense outlined")
                # Import/Export (2026-08-06) -- move templates between
                # installs/users. Both open the browser's own file
                # dialog: Import via the upload picker in the dialog
                # below, Export via a download (Save As).
                ui.button(
                    "Import Templates", icon="upload_file",
                    on_click=_open_import_dialog,
                ).classes("text-xs bg-blue-800 text-white px-3") \
                    .props("dense unelevated").tooltip(
                        "Load templates from a template file shared by another "
                        "user. Existing templates of the same name are kept "
                        "unless you tick Overwrite."
                    )
                ui.button(
                    "Export Templates", icon="download",
                    on_click=_export_templates,
                ).classes("text-xs bg-blue-900 text-white px-3") \
                    .props("dense unelevated").tooltip(
                        "Save every template in the Load list to a "
                        f"{et.EXPORT_EXTENSION} file you can share or keep as a backup."
                    )

            # ── Section header helper ────────────────────────────────────
            # Every major block below is its own bordered card with a
            # colour-coded header -- previously these floated directly on
            # the page background with identical flat-gray labels, which
            # is what made the whole editor read as one undifferentiated
            # wall of fields. Anchor TP and Pending TP get their own
            # accent colours specifically so the two ladders are
            # distinguishable at a glance without reading every label.
            def _section(title: str, color: str, tip: str = ""):
                card = ui.card().classes(
                    "w-full bg-gray-900 border border-gray-700 rounded-lg p-3 mb-2"
                )
                with card:
                    with ui.row().classes("items-center gap-1 mb-1"):
                        ui.label(title).classes(
                            f"text-xs font-bold uppercase tracking-wider {color}"
                        )
                        if tip:
                            ui.icon("info_outline", size="14px").classes(
                                "text-blue-400 cursor-help").tooltip(tip)
                return card

            # ── Entries & lots ────────────────────────────────────────────
            with _section("Entries & Lots", "text-gray-200"):
                with ui.row().classes("w-full gap-2 mb-1"):
                    def _num(key, label, step, tip, width="w-28", mn=0):
                        with ui.column().classes("gap-0"):
                            with ui.row().classes("items-center gap-1"):
                                ui.label(label).classes("text-xs text-gray-400")
                                if tip:
                                    ui.icon("info_outline", size="14px").classes(
                                        "text-blue-400 cursor-help").tooltip(tip)
                            fields[key] = ui.number(
                                value=live[key], step=step, min=mn,
                            ).classes(width).props("dense outlined")

                    _num("anchors", "Anchors", 1,
                         "How many legs enter immediately at market when the signal "
                         "arrives. The anchor takes part of the position straight "
                         "away so a signal that never retraces isn't missed entirely. "
                         "0 = pending legs only.")
                    _num("pendings", "Pendings", 1,
                         "How many resting limit legs are staged inside the signal's "
                         "entry zone, waiting for a better fill than the anchor got.")
                    _num("lot_anchor", "Anchor Lot", 0.01,
                         "Lot size for each anchor (market) leg.")
                    _num("lot_pending", "Pending Lot", 0.01,
                         "Lot size for each pending (limit) leg.")
                    _num("sl_pips", "SL (pips)", 1.0,
                         "Stop distance in pips, used when the signal doesn't supply "
                         "its own SL. 10 pips = 1.00 of gold price, so 50 = $5.00 per "
                         "0.01 lot. The signal's own SL always wins when present.")
                    _num("grid_step_pts", "Ladder Step", 1.0,
                         "Spacing between pending legs, in pips. Always used in "
                         "STEP pending mode; in ZONE mode it only applies when the "
                         "signal states no entry zone of its own.")
                    _num("risk_pct", "Risk % (0=OFF)", 0.1,
                         "Size legs from account risk instead of the fixed lots "
                         "above. 0 = use the fixed lots.")

            # ── TP ladders ────────────────────────────────────────────────
            def _tg_tp_switch(key: str, which: str):
                """"Use TP Levels from Telegram" for one ladder.

                On, the pips row below is ignored and the levels come from the
                triggering Telegram message's own TP prices. The pips values
                are kept (and stay visible, just disabled) because they are
                still what the internal signal generators use -- those have no
                message to read, so this switch never applies to them."""
                with ui.row().classes("items-center gap-2 mb-2"):
                    sw = ui.switch(
                        "Use TP Levels from Telegram",
                        value=bool(live[key]),
                    ).classes("text-xs")
                    fields[key] = sw
                    ui.icon("info_outline", size="14px").classes(
                        "text-blue-400 cursor-help").tooltip(
                        f"ON: the {which} legs take their TP levels from the "
                        f"Telegram message's own TP prices, and the pips row "
                        f"below is ignored. The message sets how many levels "
                        f"there are; the % row still decides how much closes "
                        f"at each, and (when Close Full On Last TP above is "
                        f"on) the last level closes the remainder.\n\n"
                        f"Internal signals (Reversal, Breakout, Bounce, ORB) "
                        f"have no message, so they keep using the pips row "
                        f"regardless. A Telegram signal that states no TPs "
                        f"also falls back to it.")
                return sw

            # Both ladders' R:R readouts, so shared inputs created after them
            # (SL is above, close_full_on_last is below) can refresh every one.
            _rr_refreshers: list = []

            def _ladder_grid(prefix: str, tg_switch=None) -> None:
                with ui.grid(columns=N + 1).classes("w-full gap-1"):
                    ui.label("").classes("text-xs")
                    for n in range(1, N + 1):
                        ui.label(f"TP{n}").classes("text-xs text-center text-gray-400")
                    ui.label("pips from entry").classes("text-xs text-gray-500 self-center")
                    for n in range(1, N + 1):
                        num = ui.number(
                            value=float(live[f"{prefix}{n}_pips"]), step=1.0, min=0,
                        ).classes("w-full").props("dense outlined")
                        if tg_switch is not None:
                            # Disabled, not hidden or cleared: these values
                            # still drive internal-generator trades, so they
                            # must keep their values and keep saving.
                            num.bind_enabled_from(
                                tg_switch, "value", backward=lambda v: not v)
                        fields[f"{prefix}{n}_pips"] = num
                    ui.label("% of trade to close").classes("text-xs text-gray-500 self-center")
                    for n in range(1, N + 1):
                        fields[f"{prefix}{n}_pct"] = ui.number(
                            value=float(live[f"{prefix}{n}_pct"]), step=1.0, min=0, max=100,
                        ).classes("w-full").props("dense outlined")

                    # ── R:R readout ───────────────────────────────────────
                    # Reward per unit of risk, so the ladder can be judged
                    # against the stop it is actually running rather than by
                    # eye. Two rows because they answer different questions:
                    # "R at TP" is how far that level reaches (pips / SL), and
                    # is unchanged by how much closes there; "weighted" is
                    # what that level actually contributes once only its own
                    # slice of the position is banked. A ladder can look
                    # generous on the first row and give back most of it on
                    # the second -- Asian Reversal - ATR reaches 2.50R at TP5
                    # but contributes 0.25R, because only 10% is left by then.
                    ui.label("R at TP").classes("text-xs text-gray-500 self-center")
                    rr_cells = {}
                    for n in range(1, N + 1):
                        rr_cells[n] = ui.label("—").classes(
                            "text-xs text-center text-gray-400 self-center")
                    ui.label("weighted R").classes("text-xs text-gray-500 self-center")
                    w_cells = {}
                    for n in range(1, N + 1):
                        w_cells[n] = ui.label("—").classes(
                            "text-xs text-center text-emerald-400 self-center")

                total_lbl = ui.label("").classes("text-xs mt-1")

                def _refresh_rr() -> None:
                    """Recompute the readout from whatever is in the boxes now."""
                    try:
                        sl = float(fields["sl_pips"].value or 0)
                    except (TypeError, ValueError):
                        sl = 0.0

                    # close_full_on_last is created further down the form, so
                    # fall back to the stored value until it exists.
                    _cfol_field = fields.get("close_full_on_last")
                    close_full = bool(_cfol_field.value if _cfol_field is not None
                                      else live["close_full_on_last"])

                    levels, pct_sum = [], 0.0
                    for n in range(1, N + 1):
                        try:
                            pips = float(fields[f"{prefix}{n}_pips"].value or 0)
                            pct = float(fields[f"{prefix}{n}_pct"].value or 0)
                        except (TypeError, ValueError):
                            pips = pct = 0.0
                        if pips > 0 or pct > 0:
                            levels.append((n, pips, pct))
                            pct_sum += pct

                    from_signal = bool(tg_switch is not None and tg_switch.value)

                    for n in range(1, N + 1):
                        rr_cells[n].text = "—"
                        w_cells[n].text = "—"

                    if sl <= 0:
                        total_lbl.text = "Set an SL above to see R:R."
                        total_lbl.classes(replace="text-xs mt-1 text-gray-500")
                        return
                    if not levels:
                        total_lbl.text = "No TP levels set."
                        total_lbl.classes(replace="text-xs mt-1 text-gray-500")
                        return

                    # The arithmetic lives in core_ea_templates.ladder_rr so it
                    # is testable and cannot drift from what the EA does -- see
                    # its docstring for why the position is walked as remaining
                    # lots rather than by summing the % column.
                    calc = et.ladder_rr(sl, levels, close_full)
                    for n, rr, closed, contrib in calc["rows"]:
                        rr_cells[n].text = f"{rr:.2f}R"
                        if closed > 0:
                            w_cells[n].text = f"{contrib:.2f}R"
                    total = calc["total_r"]
                    remaining = calc["remaining"]
                    banked = 100.0 - remaining
                    bits = [f"Total {total:.2f}R if every level is hit"]
                    if pct_sum > 100.0:
                        bits.append(f"⚠ %s add to {pct_sum:.0f}% — later levels "
                                    f"can only close what is left")
                    elif remaining > 0:
                        bits.append(f"{remaining:.0f}% left running on the trail/SL")
                    if from_signal:
                        bits.append("pips come from the signal — this uses the boxes above")
                    total_lbl.text = "  •  ".join(bits) + f"  (closes {banked:.0f}%)"
                    total_lbl.classes(replace=(
                        "text-xs mt-1 " + ("text-amber-400" if pct_sum > 100.0
                                           else "text-emerald-400")))

                _watched = [fields["sl_pips"]]
                for n in range(1, N + 1):
                    _watched += [fields[f"{prefix}{n}_pips"], fields[f"{prefix}{n}_pct"]]
                if tg_switch is not None:
                    _watched.append(tg_switch)
                for _w in _watched:
                    _w.on_value_change(lambda _e: _refresh_rr())
                _rr_refreshers.append(_refresh_rr)
                _refresh_rr()

            with _section(
                "Anchor TP", "text-amber-400",
                "Targets for the anchor (market) legs, in pips from entry. "
                "These are AUTHORITATIVE -- they replace whatever TP levels "
                "the triggering signal itself stated, so this channel behaves "
                "identically regardless of message shape. A level left at 0 "
                "is simply not used. The % row is always template-driven, "
                "since a signal never states how much to close at each level.",
            ):
                _ladder_grid("tp", _tg_tp_switch("tp_from_telegram", "anchor"))
                with ui.row().classes("gap-2 mt-2"):
                    ui.button("Copy to Pending ↓",
                              on_click=lambda: _copy_ladder("tp", "tp_pen")) \
                        .classes("text-xs bg-sky-700 text-white").props("dense unelevated")
                    ui.button("↑ Copy to Anchor",
                              on_click=lambda: _copy_ladder("tp_pen", "tp")) \
                        .classes("text-xs bg-amber-600 text-white").props("dense unelevated")

            with _section(
                "Pending TP", "text-sky-400",
                "Separate targets for the resting (limit) legs. Usually set "
                "WIDER than the anchor ladder: a leg filled deeper in the "
                "zone has more room to the same structural level. With "
                "Anchor = Unified every leg shares one target PRICE, so a "
                "deeper leg automatically earns more points reaching it. "
                "Leave at 0 to reuse the anchor ladder.",
            ):
                _ladder_grid("tp_pen",
                             _tg_tp_switch("tp_pen_from_telegram", "pending"))

            # ── Strategy toggles ──────────────────────────────────────────
            strategy_section = _section("Strategy", "text-emerald-400")
            with strategy_section, ui.grid(columns=3).classes("w-full gap-2 mb-1"):
                def _toggle(key, label, opts, tip):
                    with ui.card().classes("bg-gray-900 p-2 rounded-lg"):
                        with ui.row().classes("items-center gap-1"):
                            ui.label(label).classes("text-xs text-gray-300")
                            ui.icon("info_outline", size="14px").classes(
                                "text-blue-400 cursor-help").tooltip(tip)
                        fields[key] = ui.toggle(
                            opts, value=live[key],
                        ).props("dense no-caps").classes("text-xs")

                with ui.card().classes("bg-gray-900 p-2 rounded-lg"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("TG CMD").classes("text-xs text-gray-300")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "Let Logic Keywords (CLOSE ALL / RISK FREE / TP HIT) "
                            "from the channel act on trades opened under this "
                            "template.")
                    fields["tg_cmd_enabled"] = ui.switch(
                        "", value=bool(live["tg_cmd_enabled"])).classes("text-xs")
                with ui.card().classes("bg-gray-900 p-2 rounded-lg"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Harvest").classes("text-xs text-gray-300")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "Close the whole basket once its combined floating "
                            "profit clears the harvest threshold, regardless of "
                            "individual TP levels.")
                    fields["harvest_enabled"] = ui.switch(
                        "", value=bool(live["harvest_enabled"])).classes("text-xs")
                with ui.card().classes("bg-gray-900 p-2 rounded-lg"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Close Full On Last TP").classes("text-xs text-gray-300")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "ON (default): the last CONFIGURED Anchor TP level "
                            "closes whatever remains outright, regardless of "
                            "its own %. OFF: that level closes only its own %, "
                            "leaving the remainder open to run under Trail/BE "
                            "below -- for a ladder whose %s add up to well "
                            "under 100 and is meant to leave a genuine runner "
                            "instead of being flattened at the last level.")
                    fields["close_full_on_last"] = ui.switch(
                        "", value=bool(live["close_full_on_last"])).classes("text-xs")
                    # Changes which slice the deepest level actually banks, so
                    # both ladders' R:R totals move with it.
                    fields["close_full_on_last"].on_value_change(
                        lambda _e: [r() for r in _rr_refreshers])
                    for _r in _rr_refreshers:
                        _r()
                _toggle("mode", "Mode", {"grid": "GRID", "single": "SINGLE"},
                        "GRID stages anchor + pending legs across the signal's "
                        "zone. SINGLE opens one position.")
                _toggle("pending_mode", "Pending",
                        {"zone": "ZONE", "step": "STEP"},
                        "Where GRID rests its pending legs. ZONE spreads them "
                        "across the signal's own stated entry zone, honouring the "
                        "levels the signal named -- but a leg lands on the wrong "
                        "side of the market and is skipped if price has already "
                        "left that zone. STEP places them Ladder Step pips from "
                        "the anchor instead, which is what the reference copier "
                        "does and can never be skipped for being wrong-side.")
                _toggle("tpsl_mode", "TP/SL",
                        {"off": "OFF", "on": "ON", "stealth": "STEALTH"},
                        "ON puts real SL/TP on the broker order. STEALTH keeps "
                        "targets internal to the EA so they're not visible to "
                        "the broker. OFF sets neither.")
                _toggle("anchor", "Anchor",
                        {"unified": "UNIFIED", "distributed": "DISTRIBUTED"},
                        "UNIFIED: every leg shares one breakeven and one target "
                        "PRICE measured from the group's base, so a deeper leg "
                        "earns more points. DISTRIBUTED: each leg uses its own "
                        "fill price, giving every leg equal distance.")
                _toggle("trail_mode", "Trail",
                        {"off": "OFF", "candle": "CANDLE", "step": "STEP",
                         "fractal": "FRACTAL", "tp": "TP"},
                        "How the stop follows price. TP trails to the last "
                        "cleared TP level; CANDLE/FRACTAL follow structure; "
                        "STEP uses the fixed trail distance below.")

            with strategy_section, ui.row().classes("w-full gap-2 mb-1"):
                _num("trail_distance", "Trail Dist", 1.0,
                     "Stop distance behind price for STEP trailing, in pips.")
                _num("trail_activation", "Trail Activate", 1.0,
                     "Hold the stop still until the trade is this many pips in "
                     "profit. 0 = trail from the start. Independent of Trail "
                     "Trigger below (Triggers section) -- trailing arms as soon "
                     "as EITHER condition is met, whichever comes first.")
                _num("trail_step", "Trail Step", 1.0,
                     "Minimum move before the stop is adjusted again.")
                _num("harvest_threshold", "Harvest $", 1.0,
                     "Basket floating profit (account currency) that triggers a "
                     "harvest close.")

            # ── Triggers ──────────────────────────────────────────────────
            triggers_section = _section("Triggers", "text-violet-400")
            with triggers_section, ui.row().classes("w-full gap-2 mb-1"):
                with ui.column().classes("gap-0"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("BE Mode").classes("text-xs text-gray-400")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "Where breakeven puts the stop: exactly at entry, or "
                            "entry plus a small buffer to cover costs.")
                    fields["be_mode"] = ui.select(
                        {"entry": "ENTRY", "entry_buffer": "ENTRY + BUFFER"},
                        value=live["be_mode"],
                    ).classes("w-44").props("dense outlined")
                with ui.column().classes("gap-0"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("BE Trigger").classes("text-xs text-gray-400")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "Which TP level moves the stop to breakeven. Worth "
                            "knowing: measured on this account's own trade "
                            "paths, breakeven moves REDUCED expectancy in every "
                            "configuration tested — see tools/exit_policy_lab.py.")
                    fields["be_trigger"] = ui.select(
                        {n: f"TP{n}" for n in range(1, N + 1)},
                        value=int(live["be_trigger"]),
                    ).classes("w-32").props("dense outlined")
                with ui.column().classes("gap-0"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Trail Trigger").classes("text-xs text-gray-400")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "Which TP level arms trailing, instead of (or "
                            "alongside) Trail Activate's raw pip distance above "
                            "-- whichever condition is met first. OFF leaves "
                            "Trail Activate as the only arm condition. Confirmed "
                            "live on Asian - Grid: Trail Activate's default (100 "
                            "pips) sat deeper than the template's own last "
                            "defined TP (50 pips), so the runner never armed at "
                            "all and every winning trade capped at the same "
                            "~$43 regardless of how far price actually ran.")
                    fields["tp1_trigger_level"] = ui.select(
                        {0: "OFF", **{n: f"TP{n}" for n in range(1, N + 1)}},
                        value=int(live["tp1_trigger_level"]),
                    ).classes("w-32").props("dense outlined")
                with ui.column().classes("gap-0"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Cancel Pending").classes("text-xs text-gray-400")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "Which TP level cancels any still-resting sibling "
                            "legs. OFF leaves them on the book to fill later.")
                    fields["cancel_pending_level"] = ui.select(
                        {0: "OFF", **{n: f"TP{n}" for n in range(1, N + 1)}},
                        value=int(live["cancel_pending_level"]),
                    ).classes("w-32").props("dense outlined")
                with ui.column().classes("gap-0"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Sig Guard").classes("text-xs text-gray-400")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "Block a new trade on this channel while one is "
                            "already open in the same direction. Use the pips "
                            "box beside this to only block when the open trade "
                            "is that close to the new one.")
                    fields["sig_guard"] = ui.switch(
                        "", value=bool(live["sig_guard"])).classes("text-xs")
                _num("sig_guard_pips", "Sig Guard pips", 1.0,
                     "0 = block on ANY open same-direction trade for this channel. "
                     "Above 0, only an open trade whose entry is within this many "
                     "pips blocks, so a genuinely separate setup further down the "
                     "chart can still trade. The reference copier shows this as "
                     "\"SIG GUARD: 20p\".")
                with ui.column().classes("gap-0"):
                    with ui.row().classes("items-center gap-1"):
                        ui.label("Group TP Action").classes("text-xs text-gray-400")
                        ui.icon("info_outline", size="14px").classes(
                            "text-blue-400 cursor-help").tooltip(
                            "Grid only: the first TP any leg clears cancels the "
                            "resting siblings and moves the live ones to their "
                            "own breakeven.")
                    fields["group_tp_action"] = ui.switch(
                        "", value=bool(live["group_tp_action"])).classes("text-xs")

            # ── Guards & execution ────────────────────────────────────────
            with _section("Guards & Execution", "text-rose-400"), \
                 ui.row().classes("w-full gap-2 mb-1"):
                _num("equity_protect", "Equity Protect $", 1.0,
                     "Close everything on this template if floating loss exceeds "
                     "this many account-currency units. 0 = off.")
                _num("guard_pips", "Guard pips", 1.0,
                     "Minimum distance to keep between a stop and current price. "
                     "This is what prevents a breakeven move being rejected as "
                     "an invalid stop when price has already run past entry.")
                _num("max_spread_pips", "Max Spread", 0.5,
                     "Skip the trade if the spread is wider than this at fill "
                     "time.")
                _num("late_guard_pips", "Late Guard", 1.0,
                     "Reject a signal that arrives this many pips beyond its own "
                     "zone. 0 = no guard.")
                _num("signal_max_age_sec", "Max Age (s)", 1,
                     "Ignore a signal older than this many seconds at fill time.")

            with ui.row().classes("gap-2 mt-3"):
                ui.button(
                    "Save Template", on_click=lambda: _save(name_input),
                ).classes("text-xs font-semibold bg-green-700 text-white px-4") \
                    .props("dense unelevated")
                ui.button("Send to EA", on_click=lambda: _send_to_ea()) \
                    .classes("text-xs bg-green-800 text-white").props("dense") \
                    .tooltip(
                        "Push these values to the running EA right now. Without "
                        "this they still apply, but only from the next signal "
                        "onward (a template is sent with every trade open)."
                    )
                if state["name"]:
                    ui.button(
                        "Delete", on_click=lambda: _delete(state["name"]),
                    ).classes("text-xs font-semibold bg-red-800 text-white px-4") \
                        .props("dense unelevated")
                ui.button(
                    "New", on_click=lambda: _load(None),
                ).classes("text-xs px-4").props("dense outline")

    def _save(name_input) -> None:
        name = (name_input.value or "").strip()
        if not name:
            ui.notify("Enter a template name first", type="warning")
            return
        try:
            et.save_ea_template(name, _current_values())
            ui.notify(f"Saved template '{name}'", type="positive")
            state["name"] = name
            _draw_body()
        except Exception as exc:
            ui.notify(f"Save failed: {exc}", type="negative")

    def _delete(name: str) -> None:
        et.delete_ea_template(name)
        ui.notify(f"Deleted template '{name}'", type="info")
        _load(None)

    _draw_body()
