"""register_canonical_channel() (core_db_channel.py) -- lets a Telegram
listener slot beyond the two hardcoded VIP/GD2 channels (slot 3+) register
its own channel name so it shows up on the Channel Strategy tab, since
that name isn't known ahead of time the way GD VIP/GD2 are."""
import pytest

from forex_trader.core import core_db_channel as cdc


@pytest.fixture
def clean_canonical_state():
    order_snapshot = list(cdc.CANONICAL_CHANNEL_ORDER)
    names_snapshot = dict(cdc.CANONICAL_CHANNELS)
    yield
    cdc.CANONICAL_CHANNEL_ORDER[:] = order_snapshot
    cdc.CANONICAL_CHANNELS.clear()
    cdc.CANONICAL_CHANNELS.update(names_snapshot)


def test_registers_new_channel(clean_canonical_state):
    cdc.register_canonical_channel("My Third Channel")
    assert "My Third Channel" in cdc.CANONICAL_CHANNEL_ORDER
    assert cdc._canonical("My Third Channel") == "My Third Channel"


def test_blank_name_is_noop(clean_canonical_state):
    before = list(cdc.CANONICAL_CHANNEL_ORDER)
    cdc.register_canonical_channel("")
    cdc.register_canonical_channel(None)
    assert cdc.CANONICAL_CHANNEL_ORDER == before


def test_already_canonical_is_noop(clean_canonical_state):
    before_len = len(cdc.CANONICAL_CHANNEL_ORDER)
    cdc.register_canonical_channel("Gold Diggers VIP")
    assert len(cdc.CANONICAL_CHANNEL_ORDER) == before_len


def test_existing_variant_maps_to_canonical_not_duplicated(clean_canonical_state):
    before_len = len(cdc.CANONICAL_CHANNEL_ORDER)
    cdc.register_canonical_channel("GOLD DIGGERS 2.0 ⚡️")
    assert len(cdc.CANONICAL_CHANNEL_ORDER) == before_len
    assert "GOLD DIGGERS 2.0 ⚡️" not in cdc.CANONICAL_CHANNEL_ORDER


def test_repeated_registration_is_idempotent(clean_canonical_state):
    cdc.register_canonical_channel("My Third Channel")
    cdc.register_canonical_channel("My Third Channel")
    assert cdc.CANONICAL_CHANNEL_ORDER.count("My Third Channel") == 1
