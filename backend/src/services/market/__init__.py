"""Market-structure primitives shared by every engine.

Price paths, liquidity levels, volume profile, VWAP, regime and order flow.
Nothing here reaches the database or the broker: these are pure functions
over candles and ticks, so they are equally usable live, in a backtest, and
in an offline study.
"""
