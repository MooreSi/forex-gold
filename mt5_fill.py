"""Send a closing order under whichever filling mode the broker accepts.

Standard library only, like mt5_orders.py: mt5_bridge.py imports this, and on
macOS the bridge runs under Wine's Python, which has none of the app's
dependencies.

Why it exists (2026-10-02): a user's broker refused IOC, and every ladder
partial failed with `Partial close failed: 10030`. retcode 10030 is
TRADE_RETCODE_INVALID_FILL -- the mode was wrong, nothing was executed.
Opening a trade already walks the modes; closing and partial-closing sent IOC
and gave up.

IOC stays first so a broker that accepts it sees exactly one request, as
before. Only 10030 moves on to the next mode: it is the one retcode that
guarantees nothing executed. No answer at all (None) is never resent, because
the close may have filled -- the same rule the open path follows.
"""
from __future__ import annotations

from typing import Any, Optional

INVALID_FILL = 10030


def send_close(mt5: Any, request: dict) -> Optional[Any]:
    """`order_send` for a close, retried under the other filling modes on 10030.

    Returns the last result, or None if MT5 gave no answer.
    """
    # getattr: a terminal build or stand-in without one of the constants must
    # still send the IOC close it always sent, not fail before sending.
    modes = [m for m in (getattr(mt5, "ORDER_FILLING_IOC", None),
                         getattr(mt5, "ORDER_FILLING_RETURN", None),
                         getattr(mt5, "ORDER_FILLING_FOK", None))
             if m is not None]
    result = None
    for mode in modes:
        request["type_filling"] = mode
        result = mt5.order_send(request)
        if result is None or result.retcode != INVALID_FILL:
            return result
    return result
