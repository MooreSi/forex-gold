"""Read-only core-DB lookup behind the executed-trade label. docs/todo/017.

`executed_label` holds the rule; this holds the SQL, so the data layer stays
the only place that speaks it. Opens the core database through a `mode=ro`
URI, as `breakout_signal_repo.fetch_main_closed_rows_ro` does. Nothing here
writes anything.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable


def closed_risk_by_signal(vantage_signal_ids: Iterable[str]) -> dict:
    """{vantage_signal_id: (net_pnl, initial_risk)} for each id with exactly
    one closed core trade that recorded a positive `initial_risk`."""
    ids = sorted({str(v) for v in vantage_signal_ids if v})
    if not ids:
        return {}
    from backend.src.db import database as _mdb
    out: dict = {}
    conn = sqlite3.connect(f"file:{_mdb._DB_PATH}?mode=ro", uri=True)
    try:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            rows = conn.execute(
                "SELECT signal_id, COUNT(*), MAX(net_pnl), MAX(initial_risk) "
                "FROM vantage_simulated_trades WHERE status='closed' "
                f"AND signal_id IN ({','.join('?' * len(chunk))}) "
                "GROUP BY signal_id", chunk).fetchall()
            for sid, n, net, risk in rows:
                if n == 1 and risk is not None and risk > 0 and net is not None:
                    out[str(sid)] = (float(net), float(risk))
    finally:
        conn.close()
    return out
