"""Read models: trade history, performance reporting, edge stats.

SELECT-only by contract. Anything that writes, and anything that can reach the
broker, belongs in another service -- see the note on core_orb_report in
docs/todo/refactor/phase-2-analytics/README.md.
"""
