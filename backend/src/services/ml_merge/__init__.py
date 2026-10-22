"""Merging labelled ML training rows between the Mac and the VPS (docs/todo/026).

The engines generate on the node that trades, so when the owner moves trading
from one node to the other each node's history covers only its own periods. A
merge copies the other node's labelled rows here, into `ml_imported_rows`,
never into the live tables (`re_signals`, `bo_signals`, `pro_snapshots`) that
every panel, ledger and outcome tracker reads.

Three datasets ("kinds"): `re` (Reversal batch model), `bo` (Breakout batch
model), `pro` (the pro-likeness corpus). Imported rows reach a model's
training set only through `union.with_imported`, and only when the kind's
switch is on. `benefit.evaluate` turns it on when a model trained with the
imported rows scores at least as well on the newest rows as one trained
without them. Off is the default and leaves every training set unchanged.

Nothing here places, closes or sizes a trade.
"""
