"""Did the EA stop working, or did we just stop hearing from it?

bugs/013, step 3, option B. Today the app infers EA health from silence:

    _HEARTBEAT_TIMEOUT_S = 8.0        # four missed 2-second pings

That says the app has not HEARD from the EA. It does not say the EA stopped
managing the trade, and the app cannot tell those apart from its side -- which
is why the bug's own "Not to do" section forbids acting on the signal. Every
option that reduces exposure depends on first knowing which of these it is.

Measured over 30 days of rotated logs (2026-09-01): four bursts on 2026-08-31,
0-9 seconds each, against five live tickets, with zero correlation to the
app's own event-loop stalls. So it is real, it is short, and it is not the app.

The EA now reports what it has actually done, and this turns the report into a
verdict. THREE outcomes, not two -- the third is the one that would otherwise
be mistaken for a fault:

  * **ran** -- the EA kept managing through the silence. Missed pings, no
    exposure, a logging problem.
  * **no_ticks** -- the EA saw no ticks, so it had nothing to manage against.
    MT5 only calls OnTick when the market moves. Not a fault, and a quiet
    market is exactly when a stall looks most alarming and matters least.
  * **stalled** -- ticks arrived and the EA did not manage them. The real
    thing, and the only one worth acting on.

The app must also keep working against an EA build that reports none of this:
the fields arrive only after the owner recompiles, and an app that breaks
against the old build would take the whole estate down at upgrade time.
"""
from __future__ import annotations

import pytest

from backend.src.services.broker import ea_health


@pytest.fixture(autouse=True)
def _clean():
    ea_health.reset()
    yield
    ea_health.reset()


def _ping(ticks, passes, managed=1):
    return {"type": "ping", "ticks": ticks, "passes": passes, "n": managed}


class TestTheVerdict:
    def test_management_continuing_through_the_silence_is_not_a_fault(self):
        ea_health.record(_ping(ticks=100, passes=50))

        verdict = ea_health.verdict_after_silence(_ping(ticks=140, passes=70))

        assert verdict.outcome == "ran"

    def test_ticks_arriving_with_no_management_is_a_stall(self):
        """The one that matters. The market moved, the EA was asked, and it
        did nothing."""
        ea_health.record(_ping(ticks=100, passes=50))

        verdict = ea_health.verdict_after_silence(_ping(ticks=140, passes=50))

        assert verdict.outcome == "stalled"

    def test_no_ticks_at_all_is_not_a_stall(self):
        """MT5 calls OnTick only when the market moves. No ticks means the EA
        was never asked to do anything -- there is nothing to manage against
        and nothing was missed."""
        ea_health.record(_ping(ticks=100, passes=50))

        verdict = ea_health.verdict_after_silence(_ping(ticks=100, passes=50))

        assert verdict.outcome == "no_ticks"

    def test_the_three_outcomes_are_actually_distinct(self):
        """Negative control for all of the above: a function returning one
        constant would satisfy any single one of them."""
        outcomes = set()
        for ticks, passes in ((140, 70), (140, 50), (100, 50)):
            ea_health.reset()
            ea_health.record(_ping(ticks=100, passes=50))
            outcomes.add(ea_health.verdict_after_silence(_ping(ticks, passes)).outcome)

        assert outcomes == {"ran", "stalled", "no_ticks"}

    def test_the_verdict_carries_the_numbers(self):
        """A verdict with no numbers cannot be argued with, and this exists to
        settle an argument about whether there is exposure."""
        ea_health.record(_ping(ticks=100, passes=50))

        verdict = ea_health.verdict_after_silence(_ping(ticks=140, passes=50))

        assert verdict.ticks == 40 and verdict.passes == 0


class TestAnOlderEaBuild:
    def test_a_ping_without_the_fields_gives_no_verdict(self):
        """The fields arrive only when the owner recompiles. Until then the
        honest answer is "unknown", not a guess in either direction."""
        ea_health.record({"type": "ping"})

        verdict = ea_health.verdict_after_silence({"type": "ping"})

        assert verdict.outcome == "unknown"

    def test_a_half_upgraded_pair_is_unknown_too(self):
        ea_health.record({"type": "ping"})

        verdict = ea_health.verdict_after_silence(_ping(ticks=140, passes=70))

        assert verdict.outcome == "unknown"

    def test_recording_a_bare_ping_never_raises(self):
        """This runs on the receive path for every message the EA sends."""
        ea_health.record({})
        ea_health.record({"type": "ping", "ticks": "not-a-number"})
        ea_health.record(None)


class TestCountersThatGoBackwards:
    def test_a_restarted_ea_is_not_reported_as_a_stall(self):
        """GetTickCount64 and both counters reset when the terminal or the EA
        restarts. A counter that went BACKWARDS means a new EA, not a stalled
        one -- and calling a restart a stall would put a fault in the log
        every time the owner reloads the chart."""
        ea_health.record(_ping(ticks=5000, passes=900))

        verdict = ea_health.verdict_after_silence(_ping(ticks=12, passes=3))

        assert verdict.outcome == "restarted"

    def test_a_restart_is_distinct_from_a_stall(self):
        ea_health.record(_ping(ticks=5000, passes=900))
        restarted = ea_health.verdict_after_silence(_ping(ticks=12, passes=3))
        ea_health.reset()
        ea_health.record(_ping(ticks=100, passes=50))
        stalled = ea_health.verdict_after_silence(_ping(ticks=140, passes=50))

        assert restarted.outcome != stalled.outcome


class TestNothingRecordedYet:
    def test_the_first_ping_ever_gives_no_verdict(self):
        """Nothing to compare against. An app that has just started has no
        opinion about what the EA did before it was watching."""
        verdict = ea_health.verdict_after_silence(_ping(ticks=100, passes=50))

        assert verdict.outcome == "unknown"


class TestTheHumanReadableLine:
    @pytest.mark.parametrize("before,after,expect", [
        ((100, 50), (140, 70), "kept managing"),
        ((100, 50), (140, 50), "did not manage"),
        ((100, 50), (100, 50), "no ticks"),
    ])
    def test_it_says_which_case_in_words(self, before, after, expect):
        """The whole point is that a human reads this and knows whether to
        care. An outcome string alone puts the interpreting back on them."""
        ea_health.record(_ping(*before))

        verdict = ea_health.verdict_after_silence(_ping(*after))

        assert expect in verdict.summary.lower()


class TestTheEaActuallyReportsIt:
    """The verdict is worthless if the numbers never arrive, and the two halves
    are in different languages with no compiler between them."""

    def _ea_source(self):
        import pathlib
        return (pathlib.Path(__file__).resolve().parents[3]
                / "mql5" / "ForexTraderBridge.mq5").read_text(
                    encoding="utf-8", errors="replace")

    def test_the_ping_carries_both_counters(self):
        src = self._ea_source()
        ping = src[src.index('SendJson("{\\"type\\":\\"ping\\"'):][:400]

        assert "ticks" in ping and "passes" in ping

    def test_the_field_names_match_what_the_app_reads(self):
        """The contract, stated once in each language. A rename on either side
        silently returns every verdict to "unknown" -- which looks like an old
        EA build rather than a bug."""
        src = self._ea_source()

        assert '\\"ticks\\":' in src
        assert '\\"passes\\":' in src

    def test_ticks_are_counted_before_the_early_return(self):
        """OnTick returns early when there is nothing tracked. Counting after
        that would report "no ticks" for a moving market, turning the one
        outcome that means "not a fault" into a lie."""
        src = self._ea_source()
        body = src[src.index("void OnTick()"):]
        body = body[:body.index("\n}")]

        assert body.index("g_tickCount++") < body.index("return;")

    def test_passes_are_counted_after_the_management_loop(self):
        """It must mean "a pass completed", not "a pass began" -- a pass that
        blocked half way through is exactly the stall being hunted, and
        counting it at the top would report it as healthy."""
        src = self._ea_source()
        body = src[src.index("void OnTick()"):]
        body = body[:body.index("\n}")]

        assert body.index("ManageTrade(") < body.index("g_managePasses++")

    def test_the_counters_are_not_reset_anywhere_mid_run(self):
        """They only mean something as monotonic counters. A reset mid-run
        would read to the app as a restart, hiding a real stall behind a
        benign-looking verdict."""
        src = self._ea_source()

        assert "g_tickCount = 0" not in src.replace("ulong  g_tickCount    = 0;", "")
        assert "g_managePasses = 0" not in src.replace("ulong  g_managePasses = 0;", "")


class TestThePingHandlerUsesIt:
    """The module can produce a verdict; these check the live path actually
    asks for one and keeps the baseline up to date.

    Mutation found this gap: deleting `_health.record(msg)` from the ping
    handler changed no test, because every test above called `record`
    directly. A baseline that is never updated makes every verdict compare
    against the first ping since the app started.
    """

    @pytest.fixture
    def node(self):
        from backend.src.services.broker.ea_bridge import _events

        class _Node(_events.EventsMixin):
            def __init__(self):
                self.sent: list = []
                self.healthy = True

            def is_ea_healthy(self):
                return self.healthy

            async def _send(self, msg):
                self.sent.append(msg)

        return _Node()

    @pytest.mark.asyncio
    async def test_a_ping_updates_the_baseline(self, node):
        import asyncio  # noqa: F401  (marker needs the plugin loaded)

        await node._dispatch(_ping(ticks=100, passes=50))

        assert ea_health.verdict_after_silence(
            _ping(ticks=140, passes=50)).outcome == "stalled"

    @pytest.mark.asyncio
    async def test_it_still_pongs(self, node):
        """The health check must not cost the heartbeat it rides on."""
        await node._dispatch(_ping(ticks=100, passes=50))

        assert node.sent == [{"type": "pong"}]

    @pytest.mark.asyncio
    async def test_a_recovery_after_a_silence_is_judged(self, node, caplog):
        import logging

        await node._dispatch(_ping(ticks=100, passes=50))
        node.healthy = False          # the app had written the EA off

        with caplog.at_level(logging.WARNING):
            await node._dispatch(_ping(ticks=140, passes=50))

        assert "did not manage" in caplog.text

    @pytest.mark.asyncio
    async def test_a_healthy_ping_is_not_narrated(self, node, caplog):
        """Negative control, and it matters: this runs every two seconds
        forever. A line per ping is 43,200 a day."""
        import logging

        await node._dispatch(_ping(ticks=100, passes=50))

        with caplog.at_level(logging.WARNING):
            await node._dispatch(_ping(ticks=140, passes=70))

        assert caplog.text.strip() == ""
