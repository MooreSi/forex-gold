"""EA Template Equity Protect -- close every open position on a
template-managed channel once that group's combined floating loss exceeds
the template's own equity_protect setting (account-currency units, the
copier's EQUITY PROTECT ($)). Existed as a template field with no
implementation until 2026-08-04.

Grouped by (tg_source, strategy) rather than by template name alone, since
two different channels can share one template and should be protected
independently -- one channel blowing through its equity_protect shouldn't
close another channel's unrelated position just because they use the same
template.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from forex_trader.core import core_ea_templates as ea_templates

log = logging.getLogger(__name__)


async def check_equity_protect(
    open_trades: list[dict], bridge: Any, close_trade_fn: Callable[[str, str], Awaitable[Any]],
) -> None:
    by_group: dict[tuple, list[dict]] = {}
    for t in open_trades:
        strategy = t.get("strategy") or ""
        if not ea_templates.is_template_override(strategy):
            continue
        if int(t.get("mt5_ticket") or 0) <= 0:
            continue
        key = (t.get("tg_source") or "", strategy)
        by_group.setdefault(key, []).append(t)
    if not by_group:
        return

    try:
        positions = await bridge.get_positions()
    except Exception:
        return
    by_ticket = {int(p["ticket"]): p for p in (positions or []) if p.get("ticket")}

    for (tg_source, strategy), trades in by_group.items():
        tpl_name = ea_templates.template_name_from_override(strategy)
        template = ea_templates.get_ea_template(tpl_name)
        if not template:
            continue
        threshold = float(template.get("equity_protect") or 0)
        if threshold <= 0:
            continue

        total = 0.0
        live_trades = []
        for t in trades:
            pos = by_ticket.get(int(t["mt5_ticket"]))
            if pos is None:
                continue
            total += float(pos.get("profit", 0) or 0)
            live_trades.append(t)
        if not live_trades or total > -threshold:
            continue

        log.warning(
            "[EquityProtect] %s / %s floating $%.2f <= -$%.2f -- closing %d position(s)",
            tg_source, tpl_name, total, threshold, len(live_trades),
        )
        for t in live_trades:
            try:
                await close_trade_fn(t["trade_id"], "equity_protect")
            except Exception as e:
                log.warning("[EquityProtect] close failed trade=%s: %s", t.get("trade_id", "")[:8], e)
