"""
Breakout / trend-following signal generator — fully isolated from the bounce engine.

  - Separate SQLite database  (breakout_signal.db)
  - Separate ML labels        (never cross-trained with bounce data)
  - No MT5 execution          (virtual tracking only until confidence threshold met)
  - Auto-starts alongside the bounce engine

Signal types generated:
  • break-and-go    — enter on the candle that closes through a key level with momentum
  • break-and-retest — wait for pullback back to the broken level and re-entry there
"""
