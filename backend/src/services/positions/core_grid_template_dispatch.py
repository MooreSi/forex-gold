"""Grid EA template dispatch helpers (2026-07-30).

A grid-mode EA template is a *resting-order* strategy: the EA stages real
BuyLimit/SellLimit legs across the signal's entry zone (mql5's
HandleOpenTemplateGrid), so MT5 itself is what waits for price -- not a
Python poll loop. That only holds if the template is handed to the EA when
the signal ARRIVES. Every path that instead queued the signal and waited
for price to re-enter the zone before dispatching threw the whole point
away: by the time the legs were staged, price was already there, and the
app carried the fill latency the resting orders exist to avoid.

The Telegram auto-execute path has had its own copy of this rule since
2026-07-28 (core_scan_messages_auto_execute.py). These helpers are the
shared form of the same question -- "is this source/strategy a grid
template?" -- for the paths that were still queueing:

  * core_pending_signal_activation.py  (manual/sync/bot/ORB-added signals)
  * breakout_signal_live_execute.py    (Breakout Engine)
  * reversal_engine_live_execute.py    (Reversal Engine, both its normal
                                        and its LIMIT ORDER route)

Everything here is a read-only lookup; placement itself stays with the
existing open_trade/open_trade_from_signal flow, which already knows how
to hand a template to the EA.
"""
from __future__ import annotations

import logging
from typing import Optional

from backend.src.services.broker import ea_templates as ea_templates
from backend.src.db import database as db_module

log = logging.getLogger(__name__)


def grid_template(strategy: Optional[str]) -> Optional[dict]:
    """The grid-mode template `strategy` names ("template:<name>"), or None.

    None for a built-in strategy, for a template that no longer exists, and
    for a single-mode template -- single mode really is a market-fill
    strategy, so it must keep queueing exactly as it does today.
    """
    if not ea_templates.is_template_override(strategy):
        return None
    try:
        tpl = ea_templates.get_ea_template(
            ea_templates.template_name_from_override(strategy or ""))
    except Exception as exc:
        log.debug("[GridDispatch] template lookup failed for %s: %s", strategy, exc)
        return None
    if not tpl or tpl.get("mode") != "grid":
        return None
    return tpl


def grid_template_for_source(source_name: Optional[str]) -> Optional[dict]:
    """Same question, resolved from a source's own channel strategy override.

    Internal generators ("Reversal Engine", "Breakout Engine") are assigned
    a template the same way a Telegram channel is -- through
    channel_strategy_override -- so one lookup serves both. "auto" resolves
    to a built-in recommendation and can never be a template, so it is
    treated as no template rather than followed further.
    """
    if not source_name:
        return None
    try:
        override = db_module.get_channel_strategy_override(source_name)
    except Exception as exc:
        log.debug("[GridDispatch] override lookup failed for %s: %s", source_name, exc)
        return None
    if not override or override == "auto":
        return None
    return grid_template(override)
