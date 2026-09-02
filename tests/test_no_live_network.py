"""No test may make a real outbound HTTP request.

Found 2026-09-02, chasing a coverage floor that failed on Windows and not on
macOS. `services/ai/provider.py` was 35.3% here and 25.6% on CI, and the whole
difference was the DeepSeek completion body — which was being executed by
`tests/test_signal/test_engine_characterization.py`'s balance-math tests.

They were making **live, billed calls to https://api.deepseek.com** on every
local run, using the owner's own API key, twice per suite. They pass with the
network blocked, so the call was never needed for anything they assert: the
engine's close path fires AI commentary, the commentary call is wrapped in a
try/except with a fallback, and the failure was swallowed.

Four separate problems in one:

  * it costs money on every developer run;
  * it puts a third party's availability in the path of a unit test;
  * it uses whatever API key happens to be configured on that machine;
  * and it made a coverage floor that no CI runner could ever reach, because
    CI has no key — which is how it was finally noticed.

The guard lives in tests/conftest.py as an autouse fixture, with a
`live_network` marker for anything that genuinely needs the wire.
"""
from __future__ import annotations

import httpx
import pytest


class TestTheGuardIsOn:
    @pytest.mark.asyncio
    async def test_an_outbound_request_is_refused(self):
        """The property. If this ever passes silently, the suite can bill the
        owner's card again."""
        with pytest.raises(RuntimeError, match="outbound"):
            async with httpx.AsyncClient() as c:
                await c.get("https://api.deepseek.com/chat/completions")

    @pytest.mark.asyncio
    async def test_the_refusal_names_the_url(self):
        """So the next person does not have to bisect the suite to find which
        call escaped, as this one required."""
        try:
            async with httpx.AsyncClient() as c:
                await c.post("https://example.invalid/thing")
        except RuntimeError as exc:
            assert "example.invalid" in str(exc)
        else:
            pytest.fail("the request was not blocked")

    def test_a_synchronous_request_is_refused_too(self):
        """httpx.Client, not just AsyncClient — a sync call leaks just as far."""
        with pytest.raises(RuntimeError, match="outbound"):
            httpx.Client().get("https://api.deepseek.com/")


@pytest.mark.live_network
class TestTheOptOutExists:
    def test_a_marked_test_is_allowed_through(self):
        """Not exercised against a real endpoint — the point is that the
        marker disables the fixture, so a genuine integration test can exist
        without deleting the guard for everyone."""
        import tests.conftest as _c

        assert hasattr(_c, "_no_live_network")
