"""Telegram bot command routing (M4 B4).

This was `SimulationEngine._handle_bot_command`: a dict of 23 bound engine
methods, rebuilt on every incoming message, reachable only through the god
object. The parse/dispatch/error-format logic is copied verbatim; what
changed is where the handlers come from.

Two kinds of handler:

  - the 19 read-only/infra commands call service functions directly, taking
    whatever BotDeps carries (bridge, tg_reader, cfg, bot_offset...);
  - the four that can place, close, or restart something (/close,
    /marketbuy, /marketsell, /restartapp) arrive as INJECTED CALLABLES.
    Their bodies stay on the runtime, unmoved -- the order path is not
    reshaped by this refactor, and keeping them injected means this module
    can be tested end to end without an engine or a broker.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from backend.src.services.positions import core_bot_panel as bot_panel
from backend.src.services.telegram import bot_infra, bot_readonly
from backend.src.services.trading import bot_trading

log = logging.getLogger(__name__)


@dataclass
class BotDeps:
    """Everything the command table needs, bound once per loop iteration.

    Same idiom as the runtime's _make_close_trade_ctx: one authoritative
    binding site instead of 23 bound methods.
    """
    bridge: Any = None
    tg_reader: Optional[Any] = None
    cfg: Optional[dict] = None
    bot_offset: int = 0
    start_bridge_process: Optional[Callable[[], Awaitable[bool]]] = None
    # Injected runtime methods -- see module docstring.
    close_cmd: Optional[Callable[[list], Awaitable[str]]] = None
    market_buy_cmd: Optional[Callable[[list], Awaitable[str]]] = None
    market_sell_cmd: Optional[Callable[[list], Awaitable[str]]] = None
    restart_app_cmd: Optional[Callable[[list], Awaitable[str]]] = None


# command name -> async (args, deps) -> str. The adapters exist so each
# service keeps its own natural signature instead of a uniform one.
HANDLERS: dict[str, Callable[[list, BotDeps], Awaitable[str]]] = {
    "help":          lambda args, d: bot_readonly.cmd_help(args),
    "balance":       lambda args, d: bot_readonly.cmd_balance(args, d.bridge),
    "daily":         lambda args, d: bot_readonly.cmd_daily(args, d.bridge),
    "status":        lambda args, d: bot_readonly.cmd_status(args, d.bridge, d.tg_reader),
    "trades":        lambda args, d: bot_readonly.cmd_trades(args, d.bridge),
    "pause":         lambda args, d: bot_readonly.cmd_pause(args),
    "resume":        lambda args, d: bot_readonly.cmd_resume(args),
    "strategy":      lambda args, d: bot_readonly.cmd_strategy(args),
    "risk":          lambda args, d: bot_readonly.cmd_risk(args, d.bridge),
    "dpmon":         lambda args, d: bot_readonly.cmd_dpm_on(args),
    "dpmoff":        lambda args, d: bot_readonly.cmd_dpm_off(args),
    "imeon":         lambda args, d: bot_readonly.cmd_ime_on(args),
    "imeoff":        lambda args, d: bot_readonly.cmd_ime_off(args),
    "activate":      lambda args, d: bot_trading.cmd_activate(
        args, d.bridge, (d.cfg or {}).get("starting_balance", 1000.0)),
    "report":        lambda args, d: bot_trading.cmd_report(args, d.bridge, d.cfg),
    "restartbridge": lambda args, d: bot_infra.cmd_restart_bridge(
        args, d.bridge, d.start_bridge_process),
    "switchlive":    lambda args, d: bot_infra.cmd_switch_live(args, d.bridge),
    "switchdemo":    lambda args, d: bot_infra.cmd_switch_demo(args, d.bridge),
    "headless":      lambda args, d: bot_infra.cmd_headless(args, d.restart_app_cmd),
    # Injected runtime methods.
    "close":         lambda args, d: d.close_cmd(args),
    "marketbuy":     lambda args, d: d.market_buy_cmd(args),
    "marketsell":    lambda args, d: d.market_sell_cmd(args),
    "restartapp":    lambda args, d: d.restart_app_cmd(args),
}


# ── Telegram control panel (2026-08-26) ───────────────────────────────────────
# core_bot_panel calls ctx._cmd_status / _cmd_balance / _cmd_trades / _cmd_pause
# / _cmd_resume / _cmd_dpm_* / _cmd_ime_* / _cmd_activate / _cmd_report, because
# upstream's panel was handed the engine and reused its bound methods rather than
# duplicating their formatting. This branch moved every one of those onto
# bot_readonly/bot_trading as free functions, so the engine no longer has them
# and the panel would AttributeError on its first non-navigation tap.
#
# PanelCtx is the shim: the surface the panel expects, backed by the same
# functions the command table above calls. One adapter here beats editing 1,700
# lines of panel code, and it keeps the panel and the typed commands provably on
# the same implementations.

class PanelCtx:
    """The `_cmd_*` surface core_bot_panel expects, over a BotDeps."""

    def __init__(self, deps: "BotDeps") -> None:
        self._d = deps

    async def _cmd_status(self, args):   return await bot_readonly.cmd_status(args, self._d.bridge, self._d.tg_reader)
    async def _cmd_balance(self, args):  return await bot_readonly.cmd_balance(args, self._d.bridge)
    async def _cmd_trades(self, args):   return await bot_readonly.cmd_trades(args, self._d.bridge)
    async def _cmd_pause(self, args):    return await bot_readonly.cmd_pause(args)
    async def _cmd_resume(self, args):   return await bot_readonly.cmd_resume(args)
    async def _cmd_dpm_on(self, args):   return await bot_readonly.cmd_dpm_on(args)
    async def _cmd_dpm_off(self, args):  return await bot_readonly.cmd_dpm_off(args)
    async def _cmd_ime_on(self, args):   return await bot_readonly.cmd_ime_on(args)
    async def _cmd_ime_off(self, args):  return await bot_readonly.cmd_ime_off(args)
    async def _cmd_report(self, args):   return await bot_trading.cmd_report(args, self._d.bridge, self._d.cfg)

    async def _cmd_activate(self, args):
        return await bot_trading.cmd_activate(
            args, self._d.bridge, (self._d.cfg or {}).get("starting_balance", 1000.0))


async def handle_bot_command(text: str, deps: BotDeps):
    """Route an incoming message to the correct command handler."""
    if not text.startswith("/"):
        return ""
    parts = text.split()
    # Strip @BotName suffix Telegram appends in groups
    cmd  = parts[0].lstrip("/").split("@")[0].lower()
    args = parts[1:]

    # The control panel. Returns a Screen, not a string -- bot_loop renders it
    # with an inline keyboard. Upstream routed these three in engine.py's own
    # command handler, which this branch relocated here, so they arrived
    # unrouted and /panel silently did nothing.
    if cmd in ("panel", "menu", "start"):
        return bot_panel.root_screen()

    handler = HANDLERS.get(cmd)
    if handler:
        try:
            return await handler(args, deps)
        except Exception as e:
            log.warning("Bot command /%s error: %s", cmd, e)
            return f"Error running /{cmd}: {e}"
    return f"Unknown command `/{cmd}`. Send /help to see all available commands."
