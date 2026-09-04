"""request_market_analysis must give the model volatility and measured ladder
depth, not just the ladder's specification.

From a live misjudgement on 2026-09-04. The AI Analysis tab recommended
"GD VIP - Single" because its eight-rung ladder (20/40/60/80/100/120/170/270
pips) would "capture a continued move" and "let profits run". The prompt it
reasoned from contained bid/ask, spread, twenty M5 closes, session high/low
and a direction word. No ATR. No volatility measure of any kind. And no
history of where trades on that template actually finish.

Both omissions matter together, because the template's trail arms at 40 pips
(TP2) with a 50-pip distance, and 50 pips was ~42% of a typical H1 range that
morning. The rungs above TP2 are not merely unlikely -- over 30 days the
template reached TP5+ three times in 85 trades and never reached TP7 or TP8
at all. The model could not have known: it was handed the specification and
asked to reason about behaviour.

These tests pin the two inputs, and the instruction that connects them.
"""
import asyncio

import pytest

from backend.src.services.ai import claude_ai


_MINIMAL_REPLY = (
    '{"sentiment":"neutral","sentiment_confidence":0.5,"today_bias":"x",'
    '"price_low":1.0,"price_high":2.0,"key_drivers":[],"technical_summary":"x",'
    '"support_levels":[],"resistance_levels":[],"strategy_recommendation":"scale_out",'
    '"strategy_reason":"x","risk_factors":[],"signal_analysis":"x","summary":"x",'
    '"disclaimer":"AI analysis for informational purposes only. Not financial advice."}'
)


def _capture_prompt(monkeypatch, **kwargs):
    """Run request_market_analysis far enough to build its prompt, and return
    the prompt text.

    Records rather than raises: request_market_analysis wraps the provider
    call in its own broad except, so an exception thrown here is swallowed by
    the code under test and the prompt never comes back.
    """
    seen: list[str] = []

    async def _fake_complete(cfg, system, prompt, max_tokens, timeout=30):
        seen.append(prompt)
        return _MINIMAL_REPLY

    monkeypatch.setattr(claude_ai.ai_provider, "complete", _fake_complete)
    monkeypatch.setattr(claude_ai.ai_provider, "is_configured", lambda cfg: True)

    async def _no_news():
        return []
    monkeypatch.setattr(claude_ai, "_fetch_gold_news", _no_news)

    asyncio.run(claude_ai.request_market_analysis(
        tick=None, candles=[], recent_signals=[], performance={},
        cfg={"ai_provider": "deepseek", "deepseek_api_key": "k"},
        strategies=[], **kwargs,
    ))
    assert seen, "the provider was never called — no prompt was built"
    return seen[0]


def _candles(n, high, low, close):
    return [{"high": high, "low": low, "close": close, "open": close} for _ in range(n)]


def test_h1_volatility_reaches_the_prompt_in_pips(monkeypatch):
    """Pips, not price. Every template's trail and every rung is denominated
    in pips, so an ATR the model has to convert before it can compare is an
    ATR it will not compare. 1 pip = 0.10 price on this feed."""
    # 20 H1 candles each spanning 12.0 of price = 120 pips of true range
    prompt = _capture_prompt(
        monkeypatch, h1_candles=_candles(20, 4480.0, 4468.0, 4474.0),
    )

    assert "ATR" in prompt
    assert "pips" in prompt
    # 12.0 price = 120 pips; allow the renderer's rounding
    assert "120" in prompt or "119" in prompt or "121" in prompt


def test_measured_ladder_depth_reaches_the_prompt(monkeypatch):
    """The counterweight to the configuration. Without it the model argues
    from rungs that exist on paper and are never reached."""
    reach = {
        "template:GD VIP - Single": {
            "n": 85, "win_rate": 85.9, "net_pnl": 499.76,
            "no_tp": 15, "tp1_2": 50, "tp3_4": 17, "tp5_plus": 3,
            "stopped_after_tp": 62, "top_rung": 6,
        },
    }
    prompt = _capture_prompt(monkeypatch, ladder_reach=reach)

    assert "GD VIP - Single" in prompt
    # the distribution itself, so the model can see where trades finish
    assert "50" in prompt and "3" in prompt
    # and the fact that the top rungs were never reached
    assert "TP6" in prompt


def test_the_prompt_tells_the_model_a_trail_can_truncate_a_ladder(monkeypatch):
    """The two data blocks are inert without the rule that connects them.
    This sentence is the whole fix: it is what was missing when the model
    called an eight-rung ladder 'ideal for letting profits run' while its
    trail was closing trades two rungs in."""
    prompt = _capture_prompt(monkeypatch)

    low = prompt.lower()
    assert "trail" in low
    assert "truncat" in low or "cut short" in low or "before" in low


def test_it_still_builds_a_prompt_with_no_volatility_or_history(monkeypatch):
    """Both inputs are optional. The bridge goes away, the database is empty
    on a fresh install -- neither may cost the user their analysis."""
    prompt = _capture_prompt(monkeypatch)
    assert "XAUUSD" in prompt
    assert len(prompt) > 100
