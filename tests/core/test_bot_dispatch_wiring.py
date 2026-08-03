"""The Telegram bot dispatcher, after M4 B4 moved it out of the engine.

It used to be a dict of 23 bound engine methods built inside
SimulationEngine._handle_bot_command. Now it is a module-level table in
services/telegram/bot_dispatch.py driven by an explicit BotDeps, so the
routing is testable without an engine at all.

The three commands that can place or close a real order (/close, /marketbuy,
/marketsell) are NOT moved: they stay as runtime methods and arrive here as
injected callables. Every test below drives sentinels -- no MT5 object is
constructed anywhere in this file.
"""
from __future__ import annotations

import pytest

from backend.src.services.telegram import bot_dispatch


def make_deps(**overrides):
    calls = overrides.pop("_calls", [])

    async def _sentinel(name):
        async def _fn(*args, **kwargs):
            calls.append((name, args, kwargs))
            return f"ran {name}"
        return _fn

    deps = bot_dispatch.BotDeps(
        bridge="BRIDGE",
        tg_reader="READER",
        cfg={"starting_balance": 1234.0},
        bot_offset=7,
        start_bridge_process=lambda: None,
        close_cmd=None,
        market_buy_cmd=None,
        market_sell_cmd=None,
        restart_app_cmd=None,
    )
    for k, v in overrides.items():
        setattr(deps, k, v)
    return deps


@pytest.mark.asyncio
async def test_readonly_command_routes_to_its_service_function(monkeypatch):
    seen = {}

    async def fake_balance(args, bridge):
        seen["args"] = args
        seen["bridge"] = bridge
        return "BALANCE"

    monkeypatch.setattr(bot_dispatch.bot_readonly, "cmd_balance", fake_balance)
    out = await bot_dispatch.handle_bot_command("/balance", make_deps())
    assert out == "BALANCE"
    assert seen == {"args": [], "bridge": "BRIDGE"}


@pytest.mark.asyncio
async def test_at_botname_suffix_and_args_are_parsed_as_before(monkeypatch):
    seen = {}

    async def fake_strategy(args):
        seen["args"] = args
        return "OK"

    monkeypatch.setattr(bot_dispatch.bot_readonly, "cmd_strategy", fake_strategy)
    assert await bot_dispatch.handle_bot_command(
        "/strategy@ForexBot scale_out", make_deps()) == "OK"
    assert seen["args"] == ["scale_out"]


@pytest.mark.asyncio
async def test_trading_commands_route_to_the_injected_callables():
    """The order-placing commands are injected, never imported -- this is the
    seam that keeps the order path inside the runtime."""
    hits = []

    async def close_cmd(args):
        hits.append(("close", args))
        return "closed"

    async def buy_cmd(args):
        hits.append(("buy", args))
        return "bought"

    deps = make_deps(close_cmd=close_cmd, market_buy_cmd=buy_cmd)
    assert await bot_dispatch.handle_bot_command("/close 3", deps) == "closed"
    assert await bot_dispatch.handle_bot_command("/marketbuy", deps) == "bought"
    assert hits == [("close", ["3"]), ("buy", [])]


@pytest.mark.asyncio
async def test_non_command_text_is_ignored():
    assert await bot_dispatch.handle_bot_command("hello there", make_deps()) == ""


@pytest.mark.asyncio
async def test_unknown_command_message_is_unchanged():
    out = await bot_dispatch.handle_bot_command("/nope", make_deps())
    assert out == ("Unknown command `/nope`. Send /help to see all "
                   "available commands.")


@pytest.mark.asyncio
async def test_handler_exception_is_wrapped_not_raised(monkeypatch):
    async def boom(args, bridge):
        raise RuntimeError("bridge down")

    monkeypatch.setattr(bot_dispatch.bot_readonly, "cmd_balance", boom)
    out = await bot_dispatch.handle_bot_command("/balance", make_deps())
    assert out == "Error running /balance: bridge down"


def test_every_command_the_engine_dispatched_is_still_routable():
    """The table must cover exactly the 23 commands the engine's dict had."""
    assert set(bot_dispatch.HANDLERS) == {
        "help", "balance", "daily", "status", "trades", "pause", "resume",
        "close", "strategy", "risk", "marketbuy", "marketsell",
        "dpmon", "dpmoff", "imeon", "imeoff", "activate", "report",
        "restartbridge", "restartapp", "switchlive", "switchdemo", "headless",
    }


def test_runtime_no_longer_owns_the_dispatcher_or_the_delegating_commands():
    from backend.src.runtime import TradingRuntime

    assert not hasattr(TradingRuntime, "_handle_bot_command")
    for gone in ("_cmd_help", "_cmd_balance", "_cmd_daily", "_cmd_status",
                 "_cmd_trades", "_cmd_pause", "_cmd_resume", "_cmd_strategy",
                 "_cmd_risk", "_cmd_activate", "_cmd_report",
                 "_cmd_restart_bridge", "_cmd_headless", "_cmd_switch_live",
                 "_cmd_switch_demo", "_cmd_dpm_on", "_cmd_dpm_off",
                 "_cmd_ime_on", "_cmd_ime_off"):
        assert not hasattr(TradingRuntime, gone), gone
    # The order-placing handlers stay on the runtime, injected into the table.
    for kept in ("_cmd_close", "_cmd_market_price_buy",
                 "_cmd_market_price_sell", "restart_app"):
        assert hasattr(TradingRuntime, kept), kept
