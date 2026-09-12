"""The header's lifetime-P&L baseline: what this account was funded with.

Lifted out of `_header.build_header._refresh_header` (bugs/030 cause 3). It was
nested inside the header builder, which made it unreachable from a test -- the
only available check was a source-text grep, and one of those passes just as
happily with the code deleted. Nothing about the number changed in the move;
the tests in `tests/frontend/test_header_net_deposited.py` pin today's answer
so that the question underneath it can be asked safely.

**The question underneath it.** `services/broker/deposits.get_total_deposits`
computes the same figure and does not agree with this one:

| | here (the header) | `get_total_deposits` |
|---|---|---|
| counts | `type == 2` deals, credits minus debits | every deal with no `position_id` |
| cache | the caller's, in memory, 5 minutes | `app_config`, per install, 1 hour |
| gives up when | there is no credit in the window | never; 0.0 on error |

So an account that has ever taken a credit, correction or bonus (a balance
operation that is not `DEAL_TYPE_BALANCE`) will show one lifetime P&L in the
header and another on the Trading page. Routing this through the service is
three lines and would also kill the 3,650-day deal fetch behind it -- but it
moves a money figure the owner reads on every screen by an amount that cannot
be worked out from here, because it depends on deal types that live at the
broker and not in the database. It waits for him.
"""
from __future__ import annotations

from typing import Optional

# MT5's DEAL_TYPE_BALANCE. Deposits and withdrawals both land as this type and
# are told apart by the sign of `profit`.
_DEAL_TYPE_BALANCE = 2


def net_deposited_from_deals(deals) -> Optional[float]:
    """Net funding (credits minus withdrawals) from a deal history, or None.

    None means "no answer from this history", and the caller keeps whatever
    figure it had. That is the honest reading of an empty result: lifetime P&L
    is `equity - funding`, so answering 0.0 would report the entire equity of
    the account as profit the moment a history fetch came back short.
    """
    credits = 0.0
    debits = 0.0
    for d in deals or []:
        if d.get("type") != _DEAL_TYPE_BALANCE:
            continue
        profit = float(d.get("profit", 0) or 0)
        if profit > 0:
            credits += profit
        elif profit < 0:
            debits += abs(profit)
    if credits <= 0:
        return None
    return credits - debits
