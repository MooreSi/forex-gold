"""The bias-gate refusal must say WHICH rule refused it.

Seen live 2026-09-09, six times:

    [RE-Engine] bias gate blocked live exec RE-2A2BA7 -- htf now bullish vs
    direction=SELL, level_score=0.95 < 0.75

**0.95 is not < 0.75.** Two different rules share this branch:

1. `_gov.htf_bias_blocks` -- the owner's "Only trade with the trend" gate,
   which ignores `level_score` entirely;
2. the original level-score bypass, which refuses a counter-bias signal only
   when `level_score < 0.75`.

The message hardcoded the second one's reason onto both. When the owner's gate
did the refusing -- which is every one of the six above -- it printed a
comparison that is false, and credited the refusal to a rule that had not
fired.

**Why that is worse than a cosmetic slip.** It is the only visible evidence of
a money gate doing its job. Reading these lines, the natural conclusion is that
the level-score bypass is rejecting things and the trend gate is not running at
all -- which is very nearly the conclusion drawn while verifying demo 13, since
searching for the governor's own wording returns nothing.
"""
from __future__ import annotations

import inspect

from backend.src.services.reversal_engine import reversal_engine_live_execute as ex


def _body() -> str:
    src = inspect.getsource(ex)
    return "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))


class TestTheMessageDoesNotHardcodeOneRulesReason:
    def test_the_threshold_is_not_hardcoded_into_the_message(self):
        body = _body()

        assert "level_score=%.2f < 0.75" not in body, (
            "the refusal still prints '< 0.75' even when the trend gate, "
            "which never looks at level_score, is what refused it"
        )

    def test_the_reason_is_carried_into_the_log_call(self):
        """`htf_bias_blocks` already returns a sentence naming itself and the
        setting that controls it. That sentence is what should appear."""
        body = _body()
        idx = body.index("bias gate blocked live exec")
        call = body[idx:idx + 500]

        assert "_bias_block" in call or "_why" in call or "reason" in call, (
            "the log call does not carry the blocking reason"
        )


class TestTheGateItselfIsUnchanged:
    def test_both_rules_still_refuse(self):
        body = _body()

        assert "_gov.htf_bias_blocks(direction, fresh_htf, rs)" in body
        assert "level_score < 0.75" in body
