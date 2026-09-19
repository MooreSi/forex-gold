"""Set & Forget -- the swing-trading method taught by Alex G (fxalexg).

The public record of the method is a framework rather than a formula: top-down
analysis over Weekly/Daily/4H, market structure read as HH/HL or LL/LH, entries
only at an Area of Interest in line with the higher-timeframe bias, a stop
placed structurally beyond that zone, a target at the next opposing zone, a
minimum 1:2 reward-to-risk and a fixed 1-2% of the account at risk per trade.

What lives here is that framework, split so each part can be tested on its own:

    structure.py   swing points, bias, the last completed impulse
    patterns.py    the confirmation candles (engulfing, pin bar)
    aoi.py         areas of interest, and how near price is to one
    confluence.py  the scored checklist the community calls the G-Club list
    setup.py       a candidate trade, its risk-reward, and what invalidates it
    analysis.py    the orchestrator: evidence -> AI review -> setup
    prompt.py      what the model is told and what it is required to answer

**Nothing in this package places an order.** The candidate it produces is
handed to the existing money endpoints in `api/routers/orders.py` by the
browser, unchanged, after the operator has read it and pressed a button. The
only money-shaped thing here is `setup.lot_from_risk`, which forwards to
`trading/fees_sizing.suggest_lot_size` without reimplementing any of it.

Added 2026-09-19.
"""
