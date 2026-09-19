"""The Breakout engine's edge: profit factor and expectancy.

Its own module because `breakout_signal_repo.py` sits at the 800-line ceiling
and this is a different question from the ones it answers. That file records
what the engine DID; this asks whether any of it amounts to an edge -- the
same split `shadow_repo` and `trade_history_repo` already make elsewhere.

The NiceGUI app had an Edge tab (`ui/pages/edge_dashboard.py`) and the React
port had no counterpart for it anywhere.
"""
from __future__ import annotations

from backend.src.services.breakout_signal.breakout_signal_repo import get_db

__all__ = ["get_edge_stats"]


def get_edge_stats() -> dict:
    """Profit factor, expectancy, and the two averages they are built from.

    The NiceGUI Edge tab's numbers, which the React port has no counterpart
    for. A win rate on its own decides nothing: 38% with an average win three
    times the average loss is a profitable engine, and 60% with the ratio
    inverted is not. These are the figures that separate those cases.

    Gross profit and gross loss in one pass, because a browser deriving them
    from a paginated signal list would be deriving them from a page.

    `profit_factor` is None -- not 0.0 and not infinity -- for an engine that
    has never lost. A ratio with a zero denominator is not a large number, it
    is a number that does not exist yet, and 0.0 reads as the worst possible
    engine. Same rule for `expectancy` on an engine with no closed trades: a
    fresh install has no edge, not a bad one.
    """
    row = get_db().get("""
        SELECT
          COUNT(*) as closed,
          SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
          SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
          SUM(CASE WHEN pnl_dollars > 0 THEN pnl_dollars ELSE 0 END) as gross_profit,
          SUM(CASE WHEN pnl_dollars < 0 THEN -pnl_dollars ELSE 0 END) as gross_loss
        FROM bo_signals
        WHERE status='closed' AND outcome IN ('win','loss','be')
    """) or {}

    closed = int(row.get("closed") or 0)
    wins = int(row.get("wins") or 0)
    losses = int(row.get("losses") or 0)
    gross_profit = float(row.get("gross_profit") or 0.0)
    gross_loss = float(row.get("gross_loss") or 0.0)

    avg_win = (gross_profit / wins) if wins else None
    avg_loss = (gross_loss / losses) if losses else None

    if not closed:
        expectancy = None
    else:
        # Over every CLOSED trade, breakeven ones included: leaving them out
        # inflates both the win rate and this figure.
        expectancy = (gross_profit - gross_loss) / closed

    return {
        "closed": closed,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / closed * 100, 1) if closed else None,
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": (round(gross_profit / gross_loss, 2)
                          if gross_loss else None),
        "expectancy": round(expectancy, 2) if expectancy is not None else None,
        "avg_win": round(avg_win, 2) if avg_win is not None else None,
        "avg_loss": round(avg_loss, 2) if avg_loss is not None else None,
    }
