"""Turning a channel's parser on and off, against the real row.

Reported by the owner, 2026-09-21: the checkboxes on Parsing -> Channels do
nothing. They could not have: the router called

    save_channel_parser_config(channel, {**existing, "enabled": enabled})

and the real function is

    save_channel_parser_config(channel_name, parser_format, signal_prefix,
                               instant_entry_enabled, enabled, notes)

-- six required positional arguments. Every toggle raised TypeError and
answered 500, and the box sprang back on the next poll.

The router's own test did not catch it because its fake took two arguments,
which is a fake that describes a function nobody wrote. These tests use the
REAL table, and `test_the_router_writes_what_the_repo_accepts` binds the
router's call against the real signature so a fake can never disagree with it
again.

**The other five columns are carried over, not defaulted.** A toggle that
reset `parser_format` would silently re-point a channel's parser at the
default format, which changes how its messages are read -- a much larger
change than the one the operator asked for.
"""
from __future__ import annotations

import inspect

import pytest

from backend.src.services.channels import parser_repo as _parser
from backend.src.services.channels import performance as _channels


@pytest.fixture
def channel_row(fresh_db):
    _parser.save_channel_parser_config(
        "GoldSignals", "gd2", "SIGNAL:", True, True, "the good one")
    return fresh_db


def test_disabling_a_channel_writes_enabled_zero(channel_row):
    _channels.set_parser_enabled("GoldSignals", False)

    assert _parser.get_channel_parser_config("GoldSignals")["enabled"] == 0


def test_re_enabling_a_channel_writes_enabled_one(channel_row):
    _channels.set_parser_enabled("GoldSignals", False)
    _channels.set_parser_enabled("GoldSignals", True)

    assert _parser.get_channel_parser_config("GoldSignals")["enabled"] == 1


def test_toggling_keeps_every_other_column(channel_row):
    _channels.set_parser_enabled("GoldSignals", False)

    row = _parser.get_channel_parser_config("GoldSignals")
    assert row["parser_format"] == "gd2"
    assert row["signal_prefix"] == "SIGNAL:"
    assert row["instant_entry_enabled"] == 1
    assert row["notes"] == "the good one"


def test_a_channel_with_no_row_yet_gets_one(fresh_db):
    """A channel appears in the list from its stored messages, before anything
    has ever written it a parser row. Toggling that one has to create the row
    rather than fail -- otherwise the only channels that can be switched off
    are the ones already configured somewhere else."""
    _channels.set_parser_enabled("BrandNew", False)

    row = _parser.get_channel_parser_config("BrandNew")
    assert row is not None and row["enabled"] == 0


def test_the_service_returns_the_row_it_wrote(channel_row):
    """The router answers with this, and the browser renders the answer. A
    write that returned the PREVIOUS state would show the box springing back."""
    assert _channels.set_parser_enabled("GoldSignals", False)["enabled"] == 0


def test_the_router_writes_what_the_repo_accepts():
    """The bug in one assertion: whatever the router calls must bind against
    the real function. A fake with a different arity is how this shipped."""
    from backend.src.api.routers import parsing as parsing_router

    called = {}
    inspect.signature(_channels.set_parser_enabled).bind("GoldSignals", False)
    # And the controller the router actually reaches forwards the same shape.
    from backend.src.controllers import telegram_controller as tg_ctl
    inspect.signature(tg_ctl.set_channel_parser_enabled).bind("GoldSignals", False)
    assert parsing_router.tg_ctl is tg_ctl
    assert not called
