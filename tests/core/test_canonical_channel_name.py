"""The "Telegram Auto (<channel>)" wrapper must not lose a channel's settings.

Every signal row the Telegram scan path writes is stamped
`Telegram Auto (<channel>)`, while channel_performance,
channel_strategy_rec, channel_parser_config and the per-channel Trading
Schedule windows are all keyed on the bare channel name.
`CANONICAL_CHANNELS` carried an explicit decorated entry for the two
channels that existed when it was written, so exactly those two resolved
and every other channel silently did not -- its Channel Strategy pick was
ignored on the whole stored-signal path.
"""
from __future__ import annotations

import pytest

from backend.src.services.channels import repo as ch_repo


@pytest.mark.parametrize("channel", [
    "Gold Diggers VIP",                 # has an explicit decorated dict entry
    "GOLD DIGGERS INSTITUTIONAL",       # has an explicit decorated dict entry
    "Gold Diggers Scalping",            # has none -- this is the case that broke
    "Some Channel Added Tomorrow",
])
def test_the_wrapper_resolves_to_the_bare_channel(channel):
    assert ch_repo.canonical_channel_name(f"Telegram Auto ({channel})") == \
        ch_repo.canonical_channel_name(channel)


def test_a_legacy_pre_rename_name_still_folds_into_its_current_bucket():
    """The explicit dict entries do more than strip a wrapper -- they fold a
    channel's pre-rename Telegram title into the bucket it uses now, and
    that must keep working."""
    assert ch_repo.canonical_channel_name(
        "Telegram Auto (Gold Diggers 2.0)") == "GOLD DIGGERS INSTITUTIONAL"


@pytest.mark.parametrize("source", [
    "Reversal Engine", "Breakout Engine", "ORB/IVB Report", "Manual Signal", "",
])
def test_an_undecorated_source_is_returned_untouched(source):
    assert ch_repo.canonical_channel_name(source) == \
        ch_repo.CANONICAL_CHANNELS.get(source, source)


def test_an_unbalanced_wrapper_is_left_alone():
    """Only strip what is genuinely the decoration -- a channel whose own
    name happens to start with those words must not be mangled."""
    assert ch_repo.canonical_channel_name("Telegram Auto signals") == \
        "Telegram Auto signals"


def test_the_channel_strategy_override_is_found_through_the_wrapper(fresh_db):
    ch_repo.set_channel_strategy_override("Gold Diggers Scalping", "reversal_runner")
    assert ch_repo.get_channel_strategy_override(
        "Telegram Auto (Gold Diggers Scalping)") == "reversal_runner"
