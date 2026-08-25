"""EA Template Equity Protect / Basket Harvest -- close every open position
on a template-managed channel once that group's COMBINED floating P&L
crosses one of the template's own group-wide thresholds:

  equity_protect             (2026-08-04) loss direction -- copier's
                              EQUITY PROTECT ($).
  basket_harvest_threshold   (2026-08-12) profit direction -- added after a
                              basket of "Staged Ratchet 100-500" trades
                              peaked at $1,210 combined floating profit
                              (each trade's own SL ratchet was managing its
                              own risk correctly) but gave most of it back
                              to +$45.70 realized, with no mechanism to lock
                              in a strong combined swing across the whole
                              group at once.

Both are grouped by (tg_source, strategy) rather than by template name
alone, since two different channels can share one template and should be
protected/harvested independently -- one channel blowing through its
threshold shouldn't close another channel's unrelated position just
because they use the same template.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from backend.src.services.broker import ea_templates as ea_templates

log = logging.getLogger(__name__)


def _group_template_trades(open_trades: list[dict]) -> dict[tuple, list[dict]]:
    """Group open template-managed trades by (tg_source, strategy) --
    the shared unit both equity_protect and basket_harvest_threshold
    operate on."""
    by_group: dict[tuple, list[dict]] = {}
    for t in open_trades:
        strategy = t.get("strategy") or ""
        if not ea_templates.is_template_override(strategy):
            continue
        if int(t.get("mt5_ticket") or 0) <= 0:
            continue
        key = (t.get("tg_source") or "", strategy)
        by_group.setdefault(key, []).append(t)
    return by_group


async def _group_floating_totals(
    open_trades: list[dict], bridge: Any,
) -> tuple[dict[tuple, list[dict]], dict[tuple, float]] | tuple[None, None]:
    """Return (by_group, totals) -- totals[key] is the group's combined
    live floating profit. None, None when there's nothing to check or the
    positions fetch fails."""
    by_group = _group_template_trades(open_trades)
    if not by_group:
        return None, None
    try:
        positions = await bridge.get_positions()
    except Exception:
        return None, None
    by_ticket = {int(p["ticket"]): p for p in (positions or []) if p.get("ticket")}

    totals: dict[tuple, float] = {}
    live: dict[tuple, list[dict]] = {}
    for key, trades in by_group.items():
        total = 0.0
        live_trades = []
        for t in trades:
            pos = by_ticket.get(int(t["mt5_ticket"]))
            if pos is None:
                continue
            total += float(pos.get("profit", 0) or 0)
            live_trades.append(t)
        if live_trades:
            totals[key] = total
            live[key] = live_trades
    return live, totals


async def check_equity_protect(
    open_trades: list[dict], bridge: Any, close_trade_fn: Callable[[str, str], Awaitable[Any]],
) -> None:
    by_group, totals = await _group_floating_totals(open_trades, bridge)
    if not by_group:
        return

    for (tg_source, strategy), live_trades in by_group.items():
        tpl_name = ea_templates.template_name_from_override(strategy)
        template = ea_templates.get_ea_template(tpl_name)
        if not template:
            continue
        threshold = float(template.get("equity_protect") or 0)
        if threshold <= 0:
            continue

        total = totals[(tg_source, strategy)]
        if total > -threshold:
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


async def check_basket_harvest(
    open_trades: list[dict], bridge: Any, close_trade_fn: Callable[[str, str], Awaitable[Any]],
) -> None:
    """Profit-direction mirror of check_equity_protect -- see module
    docstring."""
    by_group, totals = await _group_floating_totals(open_trades, bridge)
    if not by_group:
        return

    for (tg_source, strategy), live_trades in by_group.items():
        tpl_name = ea_templates.template_name_from_override(strategy)
        template = ea_templates.get_ea_template(tpl_name)
        if not template:
            continue
        threshold = float(template.get("basket_harvest_threshold") or 0)
        if threshold <= 0:
            continue

        total = totals[(tg_source, strategy)]
        if total < threshold:
            continue

        log.info(
            "[BasketHarvest] %s / %s floating $%.2f >= $%.2f -- closing %d position(s)",
            tg_source, tpl_name, total, threshold, len(live_trades),
        )
        for t in live_trades:
            try:
                await close_trade_fn(t["trade_id"], "basket_harvest")
            except Exception as e:
                log.warning("[BasketHarvest] close failed trade=%s: %s", t.get("trade_id", "")[:8], e)
