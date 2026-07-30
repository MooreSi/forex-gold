"""Attributing EA Template sibling legs back to their trade.

A template trade opens one broker position per Anchor/Grid leg, but Python
keeps a SINGLE vantage_simulated_trades row per trade. Every leg except the
one that promoted that row therefore has no local row and no local ticket --
and the EA's order comment is the only link back that survives into MT5's own
position and deal records.

Two things went wrong because nothing followed that link:
  * History's Channel/Strategy columns were blank for those legs. Measured on
    two days of live data: 294 broker positions, only 59 with a local row.
  * The Reversal Engine reconciled its virtual balance from the single
    recorded ticket, so a 4-leg grid contributed roughly a quarter of its real
    P&L, and the signal was closed while sibling legs were still open.
"""
import pytest

from forex_trader.core.ea_bridge import (
    COMMENT_ID_LEN,
    comment_for_trade,
    trade_id_prefix_from_comment,
)


# ── Comment round-trip ────────────────────────────────────────────────────────

def test_comment_matches_what_the_ea_writes():
    """ForexTraderBridge.mq5: "ea:" + StringSubstr(trade_id, 0, 10) + kind + n."""
    assert comment_for_trade("5b88a61e-6544-4f") == "ea:5b88a61e-6"


@pytest.mark.parametrize("comment,expected", [
    ("ea:5b88a61e-6a1", "5b88a61e-6"),     # anchor leg
    ("ea:5b88a61e-6g3", "5b88a61e-6"),     # grid leg
    ("ea:0ffb044b-9g3", "0ffb044b-9"),
])
def test_leg_comments_resolve_to_the_trade_id_prefix(comment, expected):
    assert trade_id_prefix_from_comment(comment) == expected


def test_an_id_ending_in_a_leg_letter_is_not_truncated():
    """'ea:f4ef1085-aa1' is the id 'f4ef1085-a' plus leg 'a1'. Stripping the
    marker by pattern instead of by length would yield 'f4ef1085-' and match
    the wrong trade -- this is a real comment from live history."""
    assert trade_id_prefix_from_comment("ea:f4ef1085-aa1") == "f4ef1085-a"


@pytest.mark.parametrize("comment", [
    "[sl 4046.50]",     # broker-generated stop-out
    "batchClose",       # EA bulk close
    "closePosition",
    "",
    None,
    "ea:",              # prefix with no id
])
def test_foreign_comments_are_rejected(comment):
    """A comment the EA did not write must never be attributed to a trade."""
    assert trade_id_prefix_from_comment(comment) is None


def test_prefix_is_a_prefix_of_the_real_trade_id():
    """The recovered value is only the first COMMENT_ID_LEN characters, so it
    must be matched with a prefix comparison, never equality."""
    trade_id = "5b88a61e-6544-4f"
    prefix = trade_id_prefix_from_comment(comment_for_trade(trade_id))
    assert len(prefix) == COMMENT_ID_LEN
    assert trade_id.startswith(prefix)
    assert prefix != trade_id


def test_every_leg_of_one_trade_shares_a_prefix():
    trade_id = "7fd4aa52-58fd-47"
    base = comment_for_trade(trade_id)
    legs = [f"{base}a1", f"{base}g2", f"{base}g3"]
    assert {trade_id_prefix_from_comment(c) for c in legs} == {trade_id[:COMMENT_ID_LEN]}


def test_different_trades_do_not_collide():
    a = trade_id_prefix_from_comment(comment_for_trade("5b88a61e-6544-4f") + "a1")
    b = trade_id_prefix_from_comment(comment_for_trade("7fd4aa52-58fd-47") + "a1")
    assert a != b


def test_template_repair_uses_the_shared_comment_format():
    """core_template_placeholder_repair matches legs by the same comment. If
    the two ever disagreed on the id length, repair would adopt the wrong
    position or none at all."""
    from forex_trader.core import core_template_placeholder_repair as repair
    assert repair._comment_prefix("5b88a61e-6544-4f") == comment_for_trade("5b88a61e-6544-4f")
