"""First-run "Start Here" checklist (stage2 phase1/010).

Pure view-layer: the component renders a checklist from a status dict the
caller provides and navigates on "Fix this ->". It must never reach an
order/close/sizing path — the structural test at the bottom proves it, with
a negative control so the scanner itself is known to see offenders.

No test in this file can reach a broker: the component under test imports
no bridge, no engine and no service; it is data + NiceGUI markup only.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

import pytest

from frontend.components import start_here

from tests.frontend._source import module_source


def _status(**overrides):
    base = {
        "licence": True,
        "mt5_connected": True,
        "algo_enabled": True,
        "risk_set": True,
        "telegram_connected": False,
        "demo_mode": True,
    }
    base.update(overrides)
    return base


def test_checklist_rows_reflect_status():
    """Each row's done flag mirrors the status dict it was built from."""
    rows = start_here.checklist_rows(_status(mt5_connected=False, algo_enabled=False))
    by_key = {r["key"]: r for r in rows}
    assert by_key["mt5"]["done"] is False
    assert by_key["algo"]["done"] is False
    assert by_key["licence"]["done"] is True
    assert by_key["risk"]["done"] is True
    assert by_key["demo"]["done"] is True

    # Flip the same keys and the rows flip with them (negative control on the
    # mapping — a hardcoded rows list would fail here).
    rows2 = start_here.checklist_rows(_status())
    by_key2 = {r["key"]: r for r in rows2}
    assert by_key2["mt5"]["done"] is True
    assert by_key2["algo"]["done"] is True


def test_every_row_has_a_nonempty_plain_language_label():
    rows = start_here.checklist_rows(_status())
    assert len(rows) == 6
    for row in rows:
        assert row["label"].strip(), f"row {row['key']} has no label"
        assert row["hint"].strip(), f"row {row['key']} has no hint"


def test_telegram_row_is_marked_optional_and_others_are_not():
    """Telegram is explicitly optional — a red cross on an optional step would
    read as a broken setup to Simon."""
    rows = start_here.checklist_rows(_status())
    by_key = {r["key"]: r for r in rows}
    assert by_key["telegram"]["optional"] is True
    for key in ("licence", "mt5", "algo", "risk"):
        assert by_key[key]["optional"] is False, key


def test_fix_this_targets_exist():
    """Every "Fix this ->" jump lands on a tab/section that really exists —
    a renamed Settings tab must fail this test, not silently dead-end the
    user."""
    settings_src = module_source("frontend/pages/settings.py")
    app_src = module_source("frontend/app.py")
    settings_tabs = set(re.findall(r'ui\.tab\("([^"]+)"\)', settings_src))
    app_tabs = set(re.findall(r'ui\.tab\("([^"]+)"', app_src))

    rows = start_here.checklist_rows(_status())
    targeted = 0
    for row in rows:
        if row["fix_tab"] is None:
            continue
        targeted += 1
        if row["fix_tab"] == "Settings":
            assert row["fix_section"] in settings_tabs, (
                f"row {row['key']} targets Settings section {row['fix_section']!r} "
                f"which is not one of {sorted(settings_tabs)}"
            )
        else:
            assert row["fix_tab"] in app_tabs, (
                f"row {row['key']} targets top-level tab {row['fix_tab']!r} "
                f"which is not one of {sorted(app_tabs)}"
            )
    assert targeted >= 4, "most rows must offer a Fix-this jump"

    # Negative control: the scanner can see a missing target.
    assert "Not A Real Tab" not in settings_tabs
    assert "Not A Real Tab" not in app_tabs


def test_setup_seen_hides_it():
    """Shown until dismissed; dismissing persists via setup_seen."""
    assert start_here.should_show({}) is True
    assert start_here.should_show({"setup_seen": False}) is True
    # Negative control: flip the flag, the panel disappears.
    assert start_here.should_show({"setup_seen": True}) is False


def test_reads_only_no_writes():
    """Structural: the component can navigate and read status, never act.

    It must not import an engine, a bridge or any controller that can move
    money, and must not name an order/close path.
    """
    src = (REPO / "frontend" / "components" / "start_here.py").read_text(encoding="utf-8")
    forbidden = re.compile(
        r"trading_controller|open_trade|close_trade|partial_close|place_order"
        r"|backend\.src\.services|backend\.src\.db|\._bridge\b"
    )
    assert forbidden.search(src) is None, forbidden.search(src)

    # Negative control: the pattern is not blind.
    assert forbidden.search("from backend.src.services.trading import open_trade")
    assert forbidden.search("await engine._bridge.place_order(...)")


# ── The Telegram row could never be ticked (owner report, 2026-09-02) ────────

class TestTheTelegramRowReflectsTheReader:
    """It was permanently red on a fully configured install.

    `gather_status` read `tg.get("connected") or tg.get("authenticated")`, and
    `TelegramReader.get_status()` returns neither key -- it reports
    `auth_state`, `session_exists`, `api_id_set`, `api_hash_set` and `slots`.
    Both lookups returned None, so the row was False unconditionally, however
    well Telegram was set up. The same shape as a gate matching a name nothing
    uses: it looked like a check and tested nothing.
    """

    class _Reader:
        def __init__(self, state):
            self._state = state

        def get_status(self):
            return {
                "auth_state": self._state,
                "session_exists": True,
                "api_id_set": True,
                "api_hash_set": True,
                "slots": [],
            }

    @pytest.mark.asyncio
    async def test_a_connected_reader_ticks_the_row(self):
        status = await start_here.gather_status(
            None, self._Reader("connected"), demo_mode=True)

        assert status["telegram_connected"] is True

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", [
        "disconnected", "awaiting_code", "awaiting_2fa", "failed",
    ])
    async def test_an_unconnected_reader_does_not(self, state):
        status = await start_here.gather_status(
            None, self._Reader(state), demo_mode=True)

        assert status["telegram_connected"] is False

    @pytest.mark.asyncio
    async def test_reconnecting_still_counts_as_set_up(self):
        """A dropped link that is retrying is a configured install, not an
        unconfigured one. Showing it as an outstanding setup step sends the
        user to configure something that is already configured."""
        status = await start_here.gather_status(
            None, self._Reader("reconnecting"), demo_mode=True)

        assert status["telegram_connected"] is True

    @pytest.mark.asyncio
    async def test_no_reader_at_all_is_not_an_error(self):
        """The dialog opens at startup, before the reader may exist."""
        status = await start_here.gather_status(None, None, demo_mode=True)

        assert status["telegram_connected"] is False


class TestItStopsAppearingWhenEverythingIsDone:
    """Owner, 2026-09-02: "when everything is ticked it shouldn't appear".

    Previously it showed on every start until explicitly dismissed, so a
    finished setup still got the popup for ever.
    """

    def test_a_finished_checklist_does_not_show(self):
        done = {row["key"]: True for row in start_here.checklist_rows({})}

        assert start_here.should_show({}, all_done=True) is False

    def test_an_unfinished_checklist_still_shows(self):
        assert start_here.should_show({}, all_done=False) is True

    def test_dismissal_still_wins_when_things_are_outstanding(self):
        assert start_here.should_show({"setup_seen": True}, all_done=False) is False


class TestAllRowsDone:
    """What "nothing left to do" means, which is what stops the dialog."""

    _REQUIRED = {"licence": True, "mt5_connected": True, "algo_enabled": True,
                 "risk_set": True, "demo_mode": True}

    def test_every_required_row_ticked_is_done(self):
        assert start_here.all_rows_done(
            {**self._REQUIRED, "telegram_connected": True}) is True

    def test_an_install_with_nothing_set_up_is_not_done(self):
        assert start_here.all_rows_done({}) is False

    def test_the_optional_telegram_row_does_not_block_completion(self):
        """An install that never uses Telegram must still reach "done", or the
        dialog can never stop appearing for the people it applies to least.
        Asserted through all_rows_done, not by inspecting the key list: the
        first version of this test checked the set of required keys and passed
        happily with the optional filter deleted."""
        assert start_here.all_rows_done(
            {**self._REQUIRED, "telegram_connected": False}) is True

    @pytest.mark.parametrize("missing", sorted(_REQUIRED))
    def test_any_missing_required_row_blocks_completion(self, missing):
        status = {**self._REQUIRED, "telegram_connected": True, missing: False}

        assert start_here.all_rows_done(status) is False
