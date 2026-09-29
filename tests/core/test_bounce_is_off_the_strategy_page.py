"""The Bounce Engine no longer appears as a channel anywhere a channel is listed.

Owner, 2026-09-29: "bounce engine is still appearing in the strategy page, this
needs to be removed, we no longer use the bounce engine so ensure it is removed
anywhere else it may be".

Its code went on 2026-09-14 (`test_the_bounce_engine_backend_is_gone.py`), but
`channels/repo._FIXED_ENGINE_CHANNELS` still seeded it into every channel list:
Trading > Strategy, the Telegram panel's channel picker, and the channel-strategy
snapshot the sync layer sends to the paired node.

Historical trades still carry "Bounce Generator" / "Signal Generator" sources, so
two things must survive: they still fold to one canonical name, and they are
still counted as an internal engine rather than a Telegram channel.
"""
from __future__ import annotations

from backend.src.db import database as db
from backend.src.services.channels import performance
from backend.src.services.channels import repo as chan
from backend.src.services.positions import core_bot_panel as panel


def test_the_strategy_page_does_not_list_it(fresh_db):
    names = [c["source"] for c in chan.get_all_channel_strategy_settings()]
    assert "Bounce Engine" not in names
    # Negative control: the list is not simply empty.
    assert "Reversal Engine" in names


def test_even_a_leftover_override_row_does_not_bring_it_back(fresh_db):
    """The owner's database has a channel_performance row for it from when it
    ran. That row must not resurrect the entry."""
    db.set_channel_strategy_override("Bounce Engine", "conservative")
    names = [c["source"] for c in chan.get_all_channel_strategy_settings()]
    assert "Bounce Engine" not in names


def test_the_sync_snapshot_does_not_carry_it(fresh_db):
    assert "Bounce Engine" not in chan.get_all_channel_strategy_overrides()


def test_the_telegram_panel_does_not_offer_it(fresh_db):
    assert "Bounce Engine" not in [c["name"] for c in panel.channel_list()]


def test_old_trades_still_fold_to_one_name():
    for raw in ("Signal Generator", "Bounce Generator", "Bounce Engine"):
        assert chan.canonical_channel_name(raw) == "Bounce Engine", raw


def test_old_trades_are_still_an_internal_engine_not_a_channel():
    """channel_loss_cap applies to Telegram channels only; a Bounce trade in the
    ledger must not start counting against a channel cap."""
    assert "Bounce Engine" in performance.internal_engine_names()
    assert "Reversal Engine" in performance.internal_engine_names()
