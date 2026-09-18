"""One router per domain, named for the controller behind it.

A route handler is one controller (or injected-engine) call plus a response
model. No loops, no merges, no formatting, no fallbacks — the same rule a
controller is held to, for the same reason: logic that pools here is logic no
service owns, and `history_controller` acquiring three-source ledger merges is
the named example of how that ends.

200-line ceiling per router. A router that would exceed it is a signal that the
controller should expose one coarser function, not that the router needs room.
"""
