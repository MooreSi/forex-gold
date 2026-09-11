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


def _db():
    """The reversal engine's db handle, imported on use rather than at module
    load.

    `reversal_engine_repo` re-exports this module's writers from its own
    footer, so a module-level `from ... import get_db` here closes a cycle:
    import `measure_repo` first and the repo's footer runs against a
    half-built module and fails on `record_excursion`. Nothing imported it
    first until `excursion_backfill` did. Pinned by
    tests/reversal_engine/test_measure_repo_import_order.py.
    """
    from backend.src.services.reversal_engine.reversal_engine_repo import get_db
    return get_db()


# How an excursion came to be recorded. "live" is the five-second sampler in
# `_reconcile_live_signal`; "ticks" is `excursion_backfill` reconstructing the
# path from broker tick history. They have different error characteristics --
# the sampler misses any spike between two polls, the reconstruction does not
# -- so the two are labelled rather than pooled. See
# docs/todo/reversal-engine/200 section 1.2.
SOURCE_LIVE = "live"
SOURCE_TICKS = "ticks"


def record_excursion(sig_id: int, favourable_pts: float, adverse_pts: float) -> None:
    """Widen this signal's max favourable / adverse excursion watermarks.

    Both are stored as positive point distances from the entry reference.
    MAX/MIN in SQL rather than read-modify-write so a concurrent poll cannot
    narrow a watermark that another already widened, and so a NULL (first
    observation) is simply replaced.
    """
    _db().run(
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
        _db().run("UPDATE re_signals SET last_seen_sl = ? WHERE id=?",
                     round(v, 2), sig_id)




def signals_awaiting_excursion_backfill(limit: int = 500) -> list[dict]:
    """Executed, closed signals that carry no excursion yet, oldest first.

    Three filters and each one keeps a different kind of fiction out of the
    fit:

      * `live_exec_status='executed'` -- a virtual signal's path is whatever
        the engine imagined, and pooling those with real fills produces a
        distribution describing a population that never traded.
      * `status='closed'` -- an open trade's path is not finished.
      * `mfe_pts IS NULL` -- the live sampler's own watermarks are never
        overwritten. They are the only independent check on whether the
        reconstruction agrees with what was observed at the time.
    """
    rows = _db().all(
        "SELECT id, direction, trigger_price, trigger_time, close_time, "
        "       sl_dist, entry_low, entry_high "
        "FROM re_signals "
        "WHERE live_exec_status='executed' AND status='closed' "
        "  AND mfe_pts IS NULL "
        "ORDER BY close_time ASC LIMIT ?",
        limit,
    )
    return [dict(r) for r in rows]


def record_backfilled_excursion(sig_id: int, favourable_pts: float,
                                adverse_pts: float,
                                source: str = SOURCE_TICKS) -> None:
    """SET, not MAX, unlike `record_excursion`.

    A tick reconstruction sees every price the trade walked through; the live
    sampler sees one in every five seconds. Widening a watermark would be the
    right rule between two observations of the same kind and the wrong one
    here, because the reconstruction is not another sample, it is the answer.
    The WHERE clause still refuses to touch a row that already has one.
    """
    _db().run(
        "UPDATE re_signals SET mfe_pts=?, mae_pts=?, excursion_source=? "
        "WHERE id=? AND mfe_pts IS NULL",
        round(max(0.0, favourable_pts), 2), round(max(0.0, adverse_pts), 2),
        source, sig_id,
    )


def excursion_coverage() -> dict:
    """How many executed signals carry an excursion, by how it was obtained.

    `live` covers rows recorded before `excursion_source` existed as well as
    those the sampler writes now: the sampler is the only thing that ever
    wrote an excursion without a source, so treating NULL as "live" is a
    statement of fact rather than a default.
    """
    rows = _db().all(
        "SELECT COALESCE(excursion_source, ?) AS src, COUNT(*) AS n "
        "FROM re_signals "
        "WHERE live_exec_status='executed' AND mfe_pts IS NOT NULL "
        "GROUP BY src",
        SOURCE_LIVE,
    )
    return {r["src"]: r["n"] for r in rows}


def excursion_observations(limit: int = 5000) -> list[dict]:
    """Closed, live-executed signals that carry an excursion.

    The population `market/barrier_fit.fit_barriers` fits on. Live-executed
    only: a virtual signal's path is whatever the engine imagined, and
    pooling those with real fills produces a distribution describing a
    population that never traded.
    """
    rows = _db().all(
        "SELECT outcome, mfe_pts, mae_pts, atr, sl_dist, close_time, "
        "       excursion_source "
        "FROM re_signals "
        "WHERE live_exec_status='executed' AND status='closed' "
        "  AND mfe_pts IS NOT NULL AND mae_pts IS NOT NULL "
        "ORDER BY close_time DESC LIMIT ?",
        limit,
    )
    return [dict(r) for r in rows]


def closed_executed_rows(limit: int = 5000) -> list[dict]:
    """Everything `attribution.cohorts` needs, for closed live trades."""
    rows = _db().all(
        "SELECT outcome, pnl_pts, sl_dist, level_type, session, "
        "       sl_moved_to_be, created_at, trigger_time, close_time, "
        "       net_pnl_dollars, adx, atr, mt5_ticket, direction, "
        "       trigger_price, signal_ref "
        "FROM re_signals "
        "WHERE live_exec_status='executed' AND status='closed' "
        "ORDER BY close_time DESC LIMIT ?",
        limit,
    )
    return [dict(r) for r in rows]


def training_vectors(limit: int = 5000) -> list[dict]:
    """Stored ML feature vectors with the timestamp they were built at.

    What `macro_backfill` reads. `created_at` matters as much as the vector:
    the macro reading has to be the one that existed when the signal was
    generated, not the one that exists now.
    """
    rows = _db().all(
        "SELECT id, created_at, ml_features_json FROM re_signals "
        "WHERE ml_features_json IS NOT NULL AND ml_features_json != '' "
        "ORDER BY created_at DESC LIMIT ?",
        limit,
    )
    return [dict(r) for r in rows]


def write_training_vector(sig_id: int, vector_json: str) -> None:
    _db().run("UPDATE re_signals SET ml_features_json=? WHERE id=?",
              vector_json, sig_id)
