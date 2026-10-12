"""The daily AI coaching analysis: one prompt line per closed trade.

generate_daily_analysis feeds the day's closed trades to the AI provider
selected in Settings > AI and returns its JSON reply for the daily email.
Until this file, nothing exercised it: the line that formats a trade's stop
loss was an invalid f-string (`{sl:.2f if sl else 'none'}`) that raised on
EVERY closed trade, so the email never carried an AI section on a day the
account traded.

The provider is the boundary and is patched, so nothing here can reach
Anthropic or DeepSeek or spend money.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

from backend.src.services.ai import claude_ai, provider

_REPLY = {
    "overall_summary": "One stop-out, otherwise disciplined.",
    "overall_pnl_verdict": "negative",
    "best_strategy_today": "be_runner",
    "best_strategy_reason": "Protected the open profit.",
    "key_lesson": "Respect the stop.",
    "tomorrow_focus": ["Wait for the retest"],
    "trade_analysis": [],
}


def _trade(**overrides) -> dict:
    """A closed-trade row shaped like vantage_simulated_trades."""
    row = {
        "trade_id": "0fbb4d35-50ee-41",
        "direction": "BUY",
        "lot_size": 0.04,
        "entry_price": 4177.14,
        "close_price": 4172.09,
        "stop_loss": 4172.1,
        "tp1": 4181.09,
        "tp2": 4182.09,
        "exit_reason": "SL",
        "strategy": "template:30 TP1 SL50 and Trail",
        "mt5_profit": -20.2,
        "net_pnl": -20.2,
    }
    row.update(overrides)
    return row


def _analyse(trades: list[dict]) -> tuple[dict, str]:
    """Run the analysis against a fake provider; return (result, prompt sent)."""
    complete = AsyncMock(return_value=json.dumps(_REPLY))
    with patch.object(provider, "is_configured", return_value=True), \
         patch.object(provider, "complete", new=complete):
        result = asyncio.run(
            claude_ai.generate_daily_analysis(trades, 1000.0, -20.2, {}))
    return result, complete.await_args.args[2]


def test_a_trades_stop_loss_reaches_the_prompt_to_two_decimals():
    result, prompt = _analyse([_trade(stop_loss=4172.1)])

    assert "SL=4172.10 TP1=4181.09 TP2=4182.09" in prompt
    assert result == _REPLY


def test_a_trade_with_no_stop_loss_is_written_as_none():
    result, prompt = _analyse([_trade(stop_loss=None)])

    assert "SL=none TP1=4181.09" in prompt
    assert result == _REPLY


def test_a_zero_stop_loss_means_no_stop_and_is_written_as_none():
    result, prompt = _analyse([_trade(stop_loss=0.0)])

    assert "SL=none TP1=4181.09" in prompt
    assert result == _REPLY


def test_one_trade_without_a_stop_does_not_stop_the_others_being_analysed():
    protected = _trade(trade_id="aaaa1111-0000-00", stop_loss=4172.1)
    unprotected = _trade(trade_id="bbbb2222-0000-00", stop_loss=None)

    result, prompt = _analyse([protected, unprotected])

    assert "trade_id=aaaa1111-0000-00 | BUY @ entry=4177.14 close=4172.09 lots=0.04 SL=4172.10" in prompt
    assert "trade_id=bbbb2222-0000-00 | BUY @ entry=4177.14 close=4172.09 lots=0.04 SL=none" in prompt
    assert result == _REPLY
