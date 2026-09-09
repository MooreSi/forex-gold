"""A declined SL adjustment must be claimed, or it is re-evaluated forever.

**Found by running demo 17 against the demo account, 2026-09-09.** The decline
worked exactly as designed -- and logged itself 4,099 times in 71 minutes, once
a second, across five message ids:

    15:49:51 [GOLD DIGGERS INSTITUTIONAL] SL adjustment (tg_id=29402,
             via=learned_rule) to 4391.00 DECLINED — Enable SL Parsing is off
    15:49:52 ... the same line
    15:49:53 ... the same line

**This is a regression from the bugs/034 fix made earlier the same day.**
Before it, the toggle was ignored, the adjustment was applied, and
`try_claim_sl_adjustment` marked the message handled. The fix returns on the
decline *before* that claim, and the claim is the only thing that stops
`scan_messages` offering the same message on the next pass.

**It reverses a decision recorded in
`test_sl_parsing_off_blocks_adjustments.py`,** which asserted the message must
NOT be claimed so that "turning the toggle back on while it is still buffered
lets it be honoured". That reasoning traded a real, continuous cost against a
speculative benefit, and the live run showed which one is which: an unbounded
re-parse and log loop, against a window in which someone would have to flip a
setting mid-buffer. The old test is rewritten rather than deleted, so the
reversal is visible.

The decline BEHAVIOUR does not change: no stop is moved, and nothing is
substituted. Only the claim moves ahead of it.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.trading import ai_signal_fallback as af


@pytest.fixture
def spy(monkeypatch):
    """Counts claims, and refuses the second claim of the same id the way the
    real `try_claim_sl_adjustment` does."""
    seen = {"claims": 0, "declines": 0, "looked_for_trade": 0}
    claimed_ids: set = set()

    async def _to_db_thread(fn, *a, **k):
        seen["claims"] += 1
        tg_id = a[0] if a else None
        if tg_id in claimed_ids:
            return False
        claimed_ids.add(tg_id)
        return True

    monkeypatch.setattr(af.db_module, "to_db_thread", _to_db_thread)
    monkeypatch.setattr(af.trade_repo, "find_channel_open_trade",
                        lambda _c: seen.__setitem__(
                            "looked_for_trade", seen["looked_for_trade"] + 1) or None)
    return seen


def _run(on, tg_id="tg-1"):
    return asyncio.run(af.apply_sl_adjustment(
        4391.0, "GOLD DIGGERS INSTITUTIONAL", tg_id, "learned_rule",
        bridge=None, rs={"lk_enable_sl_parsing": on}))


class TestTheDeclineIsRecordedOnce:
    def test_a_declined_message_is_claimed(self, spy):
        """The claim is what stops scan_messages offering it again."""
        _run(0)

        assert spy["claims"] == 1, "the decline never claimed the message"

    def test_the_same_message_declined_twice_only_works_once(self, spy, caplog):
        """The live symptom, reproduced: the second pass must be a no-op, not
        a second decline."""
        with caplog.at_level("INFO"):
            _run(0, "tg-9")
            first = len([r for r in caplog.records if "DECLINED" in r.getMessage()])
            _run(0, "tg-9")
            total = len([r for r in caplog.records if "DECLINED" in r.getMessage()])

        assert first == 1
        assert total == 1, f"declined {total} times for one message -- this is the loop"

    def test_it_still_moves_no_stop(self, spy):
        """The whole point of bugs/034 is unchanged: nothing is adjusted."""
        _run(0)

        assert spy["looked_for_trade"] == 0


class TestTheToggleOnPathIsUnchanged:
    def test_an_allowed_adjustment_still_claims_and_proceeds(self, spy):
        _run(1)

        assert spy["claims"] == 1
        assert spy["looked_for_trade"] == 1
