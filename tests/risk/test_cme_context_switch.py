"""The CME futures context switch: reachable, off, and honest about being inert.

**Nothing in this file reaches a broker or a data vendor.** Every test calls
a pure function with a dict, or reads source text.

## What the switch is for

Spot XAUUSD on a retail MT5 feed has no tape. `services/market/order_flow.py`
already records why: the broker publishes bid/ask quotes and no Last, so there
is no trade side, and "volume" everywhere in this system is tick volume -- a
count of quote changes, not size. Every flow feature the engine has is
therefore a proxy, and `order_flow` labels each result with the method that
produced it for exactly that reason.

CME is the lit, centralised venue where gold actually prints size: GC futures
carry real volume and open interest. That is the only route by which this
engine could ever read measured flow rather than a tick-rule proxy.

## What this switch does NOT do

**Nothing, today.** There is no CME feed in this repo -- no client, no
credentials, no ingest, no column that could hold a contract's volume. This
switch records the intent and is read by one accessor that nothing consumes
yet. That is the same shape as `vol_target_sizing_enabled`, which has sat on
this card since 2026-09-11 saying "NOT YET CONNECTED to the order path".

The reason it stops here is not effort. CME market data is a paid entitlement
and choosing one is an owner decision about money and licensing that an agent
cannot make -- recorded in docs/simon-handover/039. A switch that silently
implied the engine was reading CME when it is reading nothing would be worse
than no switch at all, so the card says so in the tooltip, and
`test_the_card_admits_it_is_not_connected` keeps it saying so.
"""
from pathlib import Path

from backend.src.services.risk import capability_gates as caps

_ROOT = Path(__file__).resolve().parents[2]

# The card moved from `frontend/pages/reversal_panel/_capabilities.py` to the
# React Signal Generator tab on 2026-09-18, in the same merge that brought this
# switch across. The assertions are unchanged; only the file they read moved.
# `capabilities.ts` is the switch list as data — the same transcription-by-
# parsing the rest of the port used, so the wording below is the wording the
# operator sees.
_CARD = (_ROOT / "frontend/src/components/engines/content/capabilities.ts")


class TestTheDefaultIsOff:

    def test_an_empty_settings_row_reads_as_off(self):
        """rules/60: a new tunable changes nothing until a human moves it."""
        assert caps.cme_context_enabled({}) is False

    def test_a_row_that_predates_the_migration_reads_as_off(self):
        """A client that has not run migration 46 must behave as it did,
        not crash and not silently enable something."""
        assert caps.cme_context_enabled({"re_atr_barriers_enabled": 1}) is False

    def test_a_null_column_reads_as_off(self):
        assert caps.cme_context_enabled({"re_cme_context_enabled": None}) is False


class TestTheSwitchIsActuallyWired:

    def test_on_is_on(self):
        assert caps.cme_context_enabled({"re_cme_context_enabled": 1}) is True

    def test_the_accessor_reads_its_own_column_and_not_a_neighbour(self):
        """Negative control: the two tests above also pass for an accessor
        hardcoded to the wrong key, as long as that key is absent."""
        assert caps.cme_context_enabled({"re_cme_context_enabled": 0,
                                         "re_ai_tuning_enabled": 1}) is False


class TestTheSwitchIsReachable:
    """A switch nobody can turn on is not a switch -- migration 41 shipped
    fourteen of those on 2026-09-11 and had to be followed by a card."""

    def test_a_migration_adds_the_column_defaulting_to_off(self):
        from backend.migrations.steps import MIGRATIONS

        adds = [stmt
                for _n, _t, step in MIGRATIONS
                if isinstance(step, (list, tuple))
                for stmt in step
                if isinstance(stmt, str)
                and "ADD COLUMN re_cme_context_enabled" in stmt]

        assert len(adds) == 1, adds
        assert "vantage_risk_settings" in adds[0]
        assert "DEFAULT 0" in adds[0]

    def test_the_card_reads_and_writes_it(self):
        """The switch is on the card, and the card's checkbox is bound to the
        settings row in both directions.

        Two files, because the React tab splits them: the key lives in the
        capability list and the read/write is the shared renderer. Asserting
        only the list would pass for a list nothing renders — which is the
        exact shape of the 2026-09-11 failure this class is named after.
        """
        src = _CARD.read_text(encoding="utf-8")
        assert '"re_cme_context_enabled"' in src

        section = (_ROOT / "frontend/src/components/engines/internal"
                   / "CapabilitiesSection.tsx").read_text(encoding="utf-8")
        assert "CAPABILITIES.map" in section
        assert "checked={on(settings, cap.key)}" in section
        assert "onSave(cap.key," in section


class TestTheCardDoesNotOverclaim:

    def test_the_card_admits_it_is_not_connected(self):
        """The failure this guards against is the owner turning it on,
        seeing no change, and concluding the engine is broken -- or worse,
        believing a later decision was informed by CME data."""
        src = _CARD.read_text(encoding="utf-8")
        at = src.index("re_cme_context_enabled")
        near = src[at:at + 2000].lower()

        assert "no cme feed" in near
        assert "changes nothing" in near

    def test_the_ai_tuner_cannot_touch_it(self):
        """The AI tunes against measured evidence from this account's own
        trade history. There is no CME evidence to tune against, so handing
        it this switch would be handing it a coin to flip."""
        from backend.src.services.reversal_engine import ai_tuner

        assert "re_cme_context_enabled" not in ai_tuner.TUNABLE
