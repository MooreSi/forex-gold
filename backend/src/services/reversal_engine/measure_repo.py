"""How a live signal's excursion and stop are recorded while it runs.

Split out of `reversal_engine_repo.py` on 2026-09-10, which was at its 800-line
ceiling. These two belong together: both are sampled on the same five-second
tick in `reversal_engine_manage._reconcile_live_signal`, both write only to
`re_signals`, and both exist to serve the same question --
[reversal-engine/020](../../../../docs/todo/reversal-engine/020-losses-exceed-the-stop.md),
whether losses really exceed the stop.

Neither ever raises into the caller's hands: a measurement must not cost a live
trade its management.
"""
from __future__ import annotations

from backend.src.services.reversal_engine.reversal_engine_repo import get_db


def record_excursion(sig_id: int, favourable_pts: float, adverse_pts: float) -> None:
    """Widen this signal's max favourable / adverse excursion watermarks.

    Both are stored as positive point distances from the entry reference.
    MAX/MIN in SQL rather than read-modify-write so a concurrent poll cannot
    narrow a watermark that another already widened, and so a NULL (first
    observation) is simply replaced.
    """
    get_db().run(
        "UPDATE re_signals SET "
        "  mfe_pts = MAX(COALESCE(mfe_pts, 0), ?), "
        "  mae_pts = MAX(COALESCE(mae_pts, 0), ?) "
        "WHERE id=?",
        round(max(0.0, favourable_pts), 2), round(max(0.0, adverse_pts), 2), sig_id,
    )


def record_last_seen_sl(sig_id: int, sl: float) -> None:
    """The stop currently on the broker's position. NOT a watermark -- last
    value wins, because a trail moves it and the useful number is the one in
    force at close. Zero/absent ignored: MT5 reports 0.0 for "no stop"."""
    try:
        v = float(sl or 0.0)
    except (TypeError, ValueError):
        return
    if v > 0.0:
        get_db().run("UPDATE re_signals SET last_seen_sl = ? WHERE id=?",
                     round(v, 2), sig_id)


