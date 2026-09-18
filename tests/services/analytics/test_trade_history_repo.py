"""The SELECT-only queries behind the trade-history views.

These had no tests. They were covered incidentally, by NiceGUI page render
tests that imported `pages/history` and let it run its own queries — so the
lines executed and nothing about the results was ever asserted. The React port
deleted those pages, which is what made the gap visible; it did not create it.

What is pinned here is what the module's own docstrings **claim**, because that
is the contract a reader relies on:

* the windowed queries are windowed, and on the PARENT's open_time for legs;
* `ticket_strategies` is a LEFT JOIN, so a trade with no DPM row survives;
* `all_ticket_info` is deliberately unwindowed;
* `ticket_groups` returns parents as tier 1 and only for trades that have legs;
* a ticket with no local row is attributed from the broker's own comment.

Every ladder-leg query exists because legs 2+ have a `vantage_ladder_legs` row
and no `vantage_simulated_trades` row of their own, so a query against trades
alone silently omits them. That is the failure these tests are really guarding:
it is invisible — the view renders, with fewer rows.

Nothing here can reach a broker. The only I/O is the temporary SQLite file
`fresh_db` creates.
"""
from __future__ import annotations

import pytest

from backend.src.services.analytics import trade_history_repo as repo

from ._seed import DAY, NOW, dpm_row, leg, signal, trade


def _tuples(rows) -> list[tuple]:
    """sqlite3.Row is not comparable to a tuple. Compare on values."""
    return [tuple(r) for r in rows]


def test_no_test_in_this_file_can_reach_a_broker(fresh_db):
    """Guard rail, asserted rather than assumed. The module imports the db
    layer and the label helpers, and nothing that speaks to MT5."""
    import backend.src.services.analytics.trade_history_repo as m

    assert not hasattr(m, "mt5")
    assert not hasattr(m, "MetaTrader5")


# ── Channel attribution ──────────────────────────────────────────────────────

class TestTicketSources:
    def test_it_returns_the_channel_for_a_ticketed_trade(self, fresh_db):
        trade("t1", ticket=111, tg_source="GoldSignals")

        assert _tuples(repo.ticket_sources(NOW - DAY)) == [(111, "GoldSignals")]

    def test_a_trade_with_no_ticket_is_omitted(self, fresh_db):
        """A trade the broker never confirmed has nothing to attribute."""
        trade("t1", ticket=None, tg_source="GoldSignals")

        assert repo.ticket_sources(NOW - DAY) == []

    def test_a_trade_older_than_the_window_is_omitted(self, fresh_db):
        """Dropping the bound turns a bounded scan into a full-table one on a
        database that grows for ever — the module says so in its docstring."""
        trade("old", ticket=111, tg_source="GoldSignals", open_time=NOW - 30 * DAY)
        trade("new", ticket=222, tg_source="GoldSignals")

        assert _tuples(repo.ticket_sources(NOW - DAY)) == [(222, "GoldSignals")]

    def test_the_window_includes_a_trade_opened_exactly_on_the_cutoff(self, fresh_db):
        """`>=`, not `>`. An off-by-one here loses whichever trade sits on the
        boundary, once, and nothing ever reports it."""
        trade("edge", ticket=111, tg_source="GoldSignals", open_time=NOW - DAY)

        assert _tuples(repo.ticket_sources(NOW - DAY)) == [(111, "GoldSignals")]


class TestTicketSourcesForLegs:
    def test_a_leg_inherits_its_parents_channel(self, fresh_db):
        """The leg row carries no tg_source of its own; the join is the only
        way it gets one."""
        trade("t1", ticket=111, tg_source="GoldSignals")
        leg("t1", tier=2, ticket=222)

        assert _tuples(repo.ticket_sources_for_legs(NOW - DAY)) == [(222, "GoldSignals")]

    def test_the_parent_itself_is_not_returned_by_the_leg_query(self, fresh_db):
        """Negative control on the pairing. If this query also returned parents,
        the caller that merges both halves would double-count every ladder."""
        trade("t1", ticket=111, tg_source="GoldSignals")
        leg("t1", tier=2, ticket=222)

        assert 111 not in [r[0] for r in repo.ticket_sources_for_legs(NOW - DAY)]

    def test_the_window_is_the_parents_open_time_not_the_legs(self, fresh_db):
        """A leg has no open_time column at all, so an old parent's legs drop
        out with it. Stated here because it is the kind of thing a later
        'improvement' to the leg schema would break silently."""
        trade("old", ticket=111, tg_source="GoldSignals", open_time=NOW - 30 * DAY)
        leg("old", tier=2, ticket=222)

        assert repo.ticket_sources_for_legs(NOW - DAY) == []

    def test_a_leg_with_no_ticket_is_omitted(self, fresh_db):
        trade("t1", ticket=111, tg_source="GoldSignals")
        leg("t1", tier=2, ticket=None)

        assert repo.ticket_sources_for_legs(NOW - DAY) == []


# ── Strategy attribution ─────────────────────────────────────────────────────

class TestTicketStrategies:
    def test_a_trade_with_no_dpm_row_still_appears(self, fresh_db):
        """The LEFT JOIN, which is the whole reason the docstring calls it out:
        "An inner join would silently drop every non-DPM trade from the history
        view." The dpm_trade_id comes back None."""
        trade("t1", ticket=111, strategy="be_runner")

        assert _tuples(repo.ticket_strategies(NOW - DAY)) == [(111, "be_runner", None)]

    def test_a_trade_with_a_dpm_row_carries_its_trade_id(self, fresh_db):
        """Negative control for the test above: proves the join actually joins,
        so "None" there means "no DPM row" rather than "the join is broken"."""
        trade("t1", ticket=111, strategy="be_runner")
        dpm_row("t1")

        assert _tuples(repo.ticket_strategies(NOW - DAY)) == [(111, "be_runner", "t1")]

    def test_a_leg_inherits_its_parents_strategy(self, fresh_db):
        trade("t1", ticket=111, strategy="trail_stop")
        leg("t1", tier=2, ticket=222)

        assert _tuples(repo.ticket_strategies_for_legs(NOW - DAY)) == [(222, "trail_stop")]


# ── Order type ───────────────────────────────────────────────────────────────

def test_order_types_carry_when_a_pending_order_was_placed(fresh_db):
    """`pending_placed_at` is what separates "resting since this morning" from
    "filled immediately", and it is None for a market order."""
    trade("mkt", ticket=111, order_type="market")
    trade("pend", ticket=222, order_type="buy_limit", pending_placed_at=NOW - 3600)

    assert sorted(_tuples(repo.ticket_order_types(NOW - DAY))) == [
        (111, "market", None),
        (222, "buy_limit", NOW - 3600),
    ]


# ── Ladder grouping ──────────────────────────────────────────────────────────

class TestTicketGroups:
    def test_a_parent_with_legs_is_tier_one_and_its_legs_keep_their_tiers(self, fresh_db):
        trade("t1", ticket=111)
        leg("t1", tier=2, ticket=222)
        leg("t1", tier=3, ticket=333)

        assert sorted(_tuples(repo.ticket_groups())) == [
            ("t1", 111, 1), ("t1", 222, 2), ("t1", 333, 3),
        ]

    def test_a_trade_with_no_legs_is_left_out_entirely(self, fresh_db):
        """A group of one is nothing to collapse. Including it would put every
        ordinary trade through the row-collapsing path for no reason."""
        trade("solo", ticket=111)

        assert repo.ticket_groups() == []

    def test_a_ticket_in_both_tables_yields_both_rows_with_their_own_tiers(self, fresh_db):
        """The parent half and the leg half are read independently, so a ticket
        recorded in both tables appears once per table, each with its own tier.
        Collapsing them would lose the tier that says which is which."""
        trade("t1", ticket=111)
        leg("t1", tier=2, ticket=111)

        assert sorted(_tuples(repo.ticket_groups())) == [("t1", 111, 1), ("t1", 111, 2)]

    def test_an_exactly_duplicated_row_is_returned_twice(self, fresh_db):
        """UNION ALL, not UNION — pinned directly.

        Honest about what this seeds: the docstring says the two halves are
        "disjoint by construction", so a leg row that matches its parent on
        trade_id, ticket AND tier should not arise in practice. The test exists
        because that is the only shape in which the two operators differ, and
        the choice between them was made for a stated reason (deduplicating
        "would only cost a sort over the whole result"). Without this,
        UNION ALL can be changed to UNION and nothing anywhere goes red.
        """
        trade("t1", ticket=111)
        leg("t1", tier=1, ticket=111)

        assert _tuples(repo.ticket_groups()) == [("t1", 111, 1), ("t1", 111, 1)]

    def test_a_leg_with_no_ticket_is_omitted(self, fresh_db):
        trade("t1", ticket=111)
        leg("t1", tier=2, ticket=None)
        leg("t1", tier=3, ticket=333)

        assert sorted(_tuples(repo.ticket_groups())) == [("t1", 111, 1), ("t1", 333, 3)]


# ── Full ticket info (unwindowed on purpose) ─────────────────────────────────

class TestAllTicketInfo:
    def test_it_ignores_the_history_window(self, fresh_db):
        """"Adding a window here would make older consolidated rows lose their
        channel and strategy labels." A regression would look like old rows
        going blank in the consolidated ledger, months later, with no error."""
        trade("ancient", ticket=111, tg_source="GoldSignals",
              strategy="scale_out", direction="SELL", open_time=NOW - 365 * DAY)

        assert _tuples(repo.all_ticket_info()) == [
            (111, "GoldSignals", "scale_out", "SELL"),
        ]

    def test_legs_are_unwindowed_too_and_inherit_the_parent(self, fresh_db):
        trade("ancient", ticket=111, tg_source="GoldSignals",
              strategy="scale_out", direction="SELL", open_time=NOW - 365 * DAY)
        leg("ancient", tier=2, ticket=222)

        assert _tuples(repo.all_ticket_info_for_legs()) == [
            (222, "GoldSignals", "scale_out", "SELL"),
        ]


# ── Strategy display label ───────────────────────────────────────────────────

class TestStrategyDisplayLabel:
    """EA Templates are user-defined, so they were never in STRATEGY_NAMES and
    fell through to "—". Confirmed live 2026-07-23: every EA Template trade
    showed a blank Strategy column."""

    def test_a_template_override_reads_as_its_template_name(self):
        assert repo._strategy_display_label("template:Grid Runner") == "Template: Grid Runner"

    def test_a_built_in_strategy_reads_as_its_label(self):
        assert repo._strategy_display_label("scale_out") == "Scale Out"

    def test_an_unknown_strategy_falls_back_to_the_placeholder(self):
        assert repo._strategy_display_label("no_such_strategy") == "—"

    def test_no_strategy_at_all_is_the_placeholder_not_an_empty_cell(self):
        assert repo._strategy_display_label("") == "—"


# ── Template grouping from broker comments ───────────────────────────────────

class TestTemplateGroupMap:
    """A template trade opens one broker position per leg but keeps a SINGLE
    local row, so every leg but one has no row and no ticket lookup finds it.
    The EA's order comment is the only link back."""

    def test_two_legs_of_one_template_become_a_group(self, fresh_db):
        trade("abcdef1234-aaaa", ticket=111)

        result = repo._template_group_map({
            111: "ea:abcdef1234a1",
            222: "ea:abcdef1234g2",
        })

        assert result == {"111": ("abcdef1234-aaaa", 1), "222": ("abcdef1234-aaaa", 2)}

    def test_the_anchor_is_tier_one_even_when_its_ticket_sorts_later(self, fresh_db):
        """"tier 1 is always the leg that promoted the local trade row". The
        rest sort by ticket number, which the broker issues in fill order — so
        a numerically smaller grid leg must NOT displace the anchor."""
        trade("abcdef1234-aaaa", ticket=999)

        result = repo._template_group_map({
            999: "ea:abcdef1234a1",
            111: "ea:abcdef1234g2",
            222: "ea:abcdef1234g3",
        })

        assert result == {
            "999": ("abcdef1234-aaaa", 1),
            "111": ("abcdef1234-aaaa", 2),
            "222": ("abcdef1234-aaaa", 3),
        }

    def test_a_prefix_with_only_one_ticket_is_left_out(self, fresh_db):
        """"A group of one is nothing to collapse" — the same rule
        `ticket_groups`' caller applies."""
        trade("abcdef1234-aaaa", ticket=111)

        assert repo._template_group_map({111: "ea:abcdef1234a1"}) == {}

    def test_a_prefix_with_no_matching_local_trade_is_left_out(self, fresh_db):
        """Negative control: without a row to name the group, there is no
        trade_id to collapse the legs under."""
        assert repo._template_group_map({
            111: "ea:zzzzzzzzzza1",
            222: "ea:zzzzzzzzzzg2",
        }) == {}

    def test_a_comment_the_ea_did_not_write_is_ignored(self, fresh_db):
        """Broker-generated comments ("[sl 4046.50]", "batchClose") and blanks
        must not form groups."""
        trade("abcdef1234-aaaa", ticket=111)

        assert repo._template_group_map({111: "[sl 2421.20]", 222: "", 333: None}) == {}


# ── Comment-based attribution ────────────────────────────────────────────────

class TestCommentAttributionMaps:
    """Recovers channel, strategy and Max TP for broker positions with no local
    row. Measured on the owner's account: of 2,498 broker positions the Closed
    Trades table has rendered, only 585 could resolve a Max TP — 77% of the
    table stuck on "...". This is the path that fixes that, so a silent
    regression here restores a bug that was visible on every screen."""

    def test_a_template_leg_is_attributed_from_its_parent_row(self, fresh_db):
        trade("abcdef1234-aaaa", ticket=111, tg_source="GoldSignals",
              strategy="template:Grid Runner", max_tp_hit="TP3")

        src, strat, max_tp = repo._comment_attribution_maps({222: "ea:abcdef1234g2"})

        assert src == {"222": "GoldSignals"}
        assert strat == {"222": "Template: Grid Runner"}
        assert max_tp == {"222": "TP3"}

    def test_max_tp_is_left_unset_when_the_parent_has_not_computed_one(self, fresh_db):
        """"Left unset when the parent hasn't been computed yet, so the leg
        keeps showing '...' and picks the real value up on a later refresh."
        Writing an empty string here would freeze the column permanently."""
        trade("abcdef1234-aaaa", ticket=111, tg_source="GoldSignals",
              strategy="template:Grid Runner", max_tp_hit=None)

        _src, _strat, max_tp = repo._comment_attribution_maps({222: "ea:abcdef1234g2"})

        assert max_tp == {}

    def test_a_sibling_row_that_has_a_max_tp_is_preferred(self, fresh_db):
        """"A template trade can leave more than one row sharing a trade_id
        prefix, and picking an arbitrary one would blank the column for legs
        whose sibling row was already computed." The ORDER BY is what chooses."""
        trade("abcdef1234-aaaa", ticket=111, tg_source="GoldSignals", max_tp_hit=None)
        trade("abcdef1234-bbbb", ticket=112, tg_source="GoldSignals", max_tp_hit="TP2")

        _src, _strat, max_tp = repo._comment_attribution_maps({222: "ea:abcdef1234g2"})

        assert max_tp == {"222": "TP2"}

    def test_a_sig_comment_is_resolved_through_the_signal_id(self, fresh_db):
        """""sig:<signal_id[:8]>" -- this app's own non-template order comment.
        A position carrying it IS ours; reaching here means the trade row lost
        its mt5_ticket link."""
        signal("sig12345-9999", source_name="GoldSignals")
        trade("t1", ticket=None, signal_id="sig12345-9999",
              tg_source="GoldSignals", strategy="be_runner", max_tp_hit="TP1")

        src, strat, max_tp = repo._comment_attribution_maps({333: "sig:sig12345"})

        assert src == {"333": "GoldSignals"}
        assert strat == {"333": "BE Runner"}
        assert max_tp == {"333": "TP1"}

    def test_a_copier_position_is_labelled_external_and_gets_no_tp_promise(self, fresh_db):
        """"Copier-EA positions are not ours and have no ladder to measure
        against at all, so they get the 'n/a' sentinel -- rendered as a plain
        dash rather than a promise of an update that will never come.\""""
        src, strat, max_tp = repo._comment_attribution_maps({444: "C7_XAUUSD_12345_ANC"})

        assert src == {"444": "Copier EA (C7)"}
        assert strat == {"444": "External"}
        assert max_tp == {"444": "n/a"}

    def test_a_copier_pending_comment_is_recognised_too(self, fresh_db):
        src, _strat, _max_tp = repo._comment_attribution_maps({445: "C12_XAUUSD_99_PEN"})

        assert src == {"445": "Copier EA (C12)"}

    def test_a_comment_matching_nothing_is_attributed_nothing(self, fresh_db):
        """Negative control. A broker's own "[sl ...]" comment must not be
        forced into one of the three shapes — a wrong channel is worse than
        "Unknown", because it is believed."""
        trade("abcdef1234-aaaa", ticket=111, tg_source="GoldSignals")

        assert repo._comment_attribution_maps({555: "[sl 2421.20]"}) == ({}, {}, {})

    def test_no_comments_at_all_returns_three_empty_maps(self, fresh_db):
        assert repo._comment_attribution_maps({}) == ({}, {}, {})
        assert repo._comment_attribution_maps(None) == ({}, {}, {})
