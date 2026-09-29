"""The shortest wait for an EA Template open's ack (owner, 2026-09-29).

`open_trade` waits 10 s + 5 s per leg for a template (at most 60 s), 5 s for a
built-in strategy, and never less than TEMPLATE_ACK_MIN_S for a template. Five
acks "timed out after 5s" on the VPS between 08:12 and 09:09 that morning,
all five had been placed, and each left a ticket-less placeholder holding a
trade slot until the repair pass adopted it. A built-in strategy keeps 5 s:
its timeout falls back to Python, and waiting longer only delays that.

Its own module because open_trade.py sits at the 800-line ceiling.
"""
from __future__ import annotations

TEMPLATE_ACK_MIN_S = 30.0
