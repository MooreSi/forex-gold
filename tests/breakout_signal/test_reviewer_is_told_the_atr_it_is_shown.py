"""The AI reviewer's prompt names the timeframe its ATR comes from (bugs/063).

The breakout engine's one ATR is computed from M5 candles and stored under
the legacy key `atr_m15`; the prompt said "ATR(M15)". Measured 2026-09-16 the
M5 figure was 0.82x the M15 one. The column name stays (legacy, 124 rows);
the prompt says M5.
"""
from backend.src.services.breakout_signal import claude_reviewer


def _prompt():
    candidate = {"direction": "BUY", "breakout_type": "level", "broken_level": 4300.0}
    risk = {"entry_mid": 4301.0, "stop_loss": 4295.0, "tp1": 4310.0, "tp2": 4315.0,
            "tp3": 4320.0, "rr_tp1": 1.5, "sl_dist": 6.0}
    return claude_reviewer._build_prompt(candidate, risk, {"atr_m15": 16.27, "price": 4301.0})


def test_the_prompt_says_m5():
    p = _prompt()
    assert "ATR(M5): 16.27" in p
    assert "ATR(M15)" not in p
