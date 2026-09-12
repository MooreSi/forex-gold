"""The header's lifetime-P&L baseline, pulled out where a test can reach it.

`bugs/030` cause 3: the header computes net deposits inline inside
`build_header._refresh_header`, from a 3,650-day deal history it fetches per
client, per page build. The same figure already exists once and shared, in
`services/broker/deposits.get_total_deposits` -- but the two count different
deals, so routing the header through the service would move a money figure the
owner reads on every screen by an amount that cannot be determined without the
broker. That is his call.

What was blocking even *asking* the question was that `_refresh_header` is
nested inside `build_header` and cannot be called from a test, so the only
available check was a source-text grep -- which passes just as happily with the
code deleted (see the `structural tests match comments` note). These tests
exist so that the header's filter is pinned behaviourally BEFORE anyone changes
it, and so the exact difference between the two implementations is written down
in something that runs.

Nothing here changes the number. Every assertion below is today's behaviour.
"""
from __future__ import annotations

from frontend.app._header_pnl import net_deposited_from_deals


class TestWhatTheHeaderCountsAsFunding:
    def test_it_nets_withdrawals_off_credits(self):
        deals = [
            {"type": 2, "profit": 1000.0},
            {"type": 2, "profit": 500.0},
            {"type": 2, "profit": -200.0},
        ]

        assert net_deposited_from_deals(deals) == 1300.0

    def test_a_trades_profit_is_not_funding(self):
        """Only DEAL_TYPE_BALANCE (2). A winning trade is equity, not a deposit
        -- counting it would make lifetime P&L subtract its own gains."""
        deals = [
            {"type": 2, "profit": 1000.0},
            {"type": 0, "profit": 250.0},
            {"type": 1, "profit": -80.0},
        ]

        assert net_deposited_from_deals(deals) == 1000.0

    def test_a_missing_profit_field_contributes_nothing(self):
        deals = [{"type": 2, "profit": 1000.0}, {"type": 2}]

        assert net_deposited_from_deals(deals) == 1000.0


class TestWhenItRefusesToAnswer:
    """`None` means "I have no figure" -- the caller keeps the last one it had
    rather than showing a lifetime P&L computed against zero funding, which on
    this account would read as the entire equity being profit."""

    def test_an_empty_history_is_no_answer(self):
        assert net_deposited_from_deals([]) is None

    def test_a_history_with_no_credit_at_all_is_no_answer(self):
        """The `credits > 0` guard, kept deliberately. It is also the reason a
        withdrawal-only history cannot move the figure: with no credit in the
        window there is nothing to net against, and answering -200.0 here would
        claim the account was funded with a negative deposit."""
        deals = [{"type": 2, "profit": -200.0}, {"type": 0, "profit": 40.0}]

        assert net_deposited_from_deals(deals) is None


class TestWhereItDiffersFromTheService:
    """`services/broker/deposits.get_total_deposits` answers the same question
    by a different filter: every deal with no `position_id`. So the two can
    disagree about one account, and the header is the narrower of the two.

    Pinned here as the difference, not as a preference. Which one is right is
    `docs/todo/bugs/030` cause 3, and it needs the owner looking at the header
    before and after -- the answer depends on deal types in the MT5 account,
    which are not in the database.
    """

    def test_a_balance_operation_that_is_not_type_2_is_invisible_here(self):
        """Credit, correction and bonus operations (DEAL_TYPE_CREDIT and
        friends) carry no position_id, so the service counts them and this does
        not. On an account that has ever taken one, the Trading page and the
        header report different lifetime funding."""
        deals = [
            {"type": 2, "profit": 1000.0},
            {"type": 4, "profit": 300.0, "position_id": 0},
        ]

        assert net_deposited_from_deals(deals) == 1000.0
