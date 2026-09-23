"""Fit the meta-labeller from history, once a day.

Until 2026-09-22 nothing called `MetaLabeller.fit`, so the live instance
said "never fitted" for as long as the app ran and the AI tuner turned its
gate off on 2026-09-15 for that reason. This is the missing caller.

**Nothing here places, closes or modifies a trade, and nothing here decides
whether the live path consults the model.** That is `meta_label_gate_enabled`
in the risk settings, read by `capability_gates.meta_label_gate`. Fitting
only changes what `meta_label.score_signal` returns: the probability the
shadow log records on every fill attempt, and the evidence the AI tuner
reads.

Why it fits at any hour, not in the 22:00 slot: the model lives in memory.
A date key in app_config would survive a restart that the model does not,
and leave the restarted app unfitted until the next evening. So the day it
last fitted is process state, and a fresh process fits on its first pass
(the research loop waits 90s after start before its first).

What the first measurement said (2026-09-22, 5,532 rows): AUC 0.553 out of
sample, which clears the 0.55 bar, and no band of its score with positive
expectancy. See `docs/system/domains/engines/README.md`.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from backend.src.services.reversal_engine import meta_label

log = logging.getLogger("reversal_engine")

# London date of the last completed fit (armed OR refused), and the cross-
# asset toggle it was fitted under. Process state on purpose -- see the
# module docstring. A change of toggle refits at once rather than next day.
_last_fit_date: Optional[str] = None
_last_toggle: Optional[bool] = None


def _load_closed_signals() -> list[dict]:
    from backend.src.services.reversal_engine import reversal_engine_repo as re_db
    return re_db.get_ml_training_data()


async def _read_settings() -> dict:
    from backend.src.services.risk import settings as _risk
    return await _risk.get_async()


def _record_fit(**kw) -> None:
    from backend.src.services.reversal_engine import xasset_repo
    xasset_repo.record_fit(**kw)


def _per_peer(signals: list[dict]) -> dict:
    """How well each peer's 60-minute move alone ranks "made money", over
    the signals where that peer was measured. The chart's per-peer line."""
    from backend.src.services.reversal_engine import cross_asset as xa
    import json
    out: dict = {}
    for peer in xa.PEERS:
        scores, labels = [], []
        for s in signals:
            try:
                stored = json.loads(s.get("xasset_json") or "")
                z = (stored.get("features") or {}).get(f"{peer}_z60")
                net = s.get("net_pnl_dollars")
            except (TypeError, ValueError, AttributeError):
                continue
            if z is None or net is None:
                continue
            scores.append(float(z))
            labels.append(1 if float(net) > 0 else 0)
        a = meta_label.auc(scores, labels)
        out[peer] = {"auc_z60": None if a is None else round(a, 4), "n": len(scores)}
    return out


async def meta_label_refit_sweep(engine: Any, now: Optional[datetime] = None,
                                 loader: Optional[Callable[[], list]] = None,
                                 settings_reader: Optional[Callable] = None,
                                 recorder: Optional[Callable[..., None]] = None) -> None:
    """One check of the timer. Fits once per London day, and again whenever
    the cross-asset toggle changes.

    Every fit measures BOTH variants -- with and without the cross-asset
    features, on the same measured signals -- and records their
    out-of-sample AUC (`re_xasset_fits`), so the impact is on the chart
    whatever the toggle says. The toggle only chooses which model is
    installed. docs/todo/reversal-engine/230.

    `engine` is accepted and unused so this matches the signature the minute
    loop calls its jobs with.
    """
    global _last_fit_date, _last_toggle
    if now is None:
        now = datetime.now(ZoneInfo("Europe/London"))
    date_str = now.strftime("%Y-%m-%d")

    try:
        from backend.src.services.risk import capability_gates as _caps
        toggle = _caps.xasset_features_enabled(await (settings_reader or _read_settings)())
    except Exception as e:                        # noqa: BLE001
        # Unreadable is OFF: the switch changes nothing until a human moves it.
        log.debug("[RE-Meta] cross-asset toggle unreadable, treating as off: %s", e)
        toggle = False

    if _last_fit_date == date_str and _last_toggle == toggle:
        return

    # No node-role check, deliberately. The model lives in this process and
    # is consulted only by this process's live path, which a remote node
    # never reaches (`_run_cycle` returns before generating anything). The
    # cost of fitting there anyway is a few seconds of CPU a day. A check
    # here would also be one more `is_remote_node` call on the shared timer,
    # which `tests/core/test_reversal_research_characterization.py` counts.

    try:
        signals = (loader or _load_closed_signals)()
    except Exception as e:                        # noqa: BLE001
        # Day left unclaimed and the current model left in place; the next
        # minute tries again.
        log.warning("[RE-Meta] could not read closed signals, not refitting: %s", e)
        return

    from backend.src.services.reversal_engine import cross_asset as _xa
    rows = meta_label.rows_from_signals(signals)
    measured = [s for s in signals if _xa.vector(s.get("xasset_json")) is not None]
    x_rows = meta_label.rows_from_signals(measured, xasset=True)
    # The base model refitted on exactly the signals the cross-asset one
    # sees, so the two AUCs differ only by the peers.
    cmp_rows = meta_label.rows_from_signals(measured)

    def _fit():
        base = meta_label.MetaLabeller()
        base.fit(rows)
        if not x_rows:
            return base, None, None, {}
        cmp_base = meta_label.MetaLabeller()
        cmp_base.fit(cmp_rows)
        with_x = meta_label.MetaLabeller(uses_xasset=True)
        with_x.fit(x_rows)
        return base, cmp_base, with_x, _per_peer(measured)

    # CPU off the event loop, which it shares with signal dispatch and
    # position management.
    base, cmp_base, with_x, per_peer = await asyncio.to_thread(_fit)

    fresh = with_x if (toggle and with_x is not None) else base
    if toggle and with_x is None:
        # On, but no signal has been measured yet: the cross-asset model has
        # nothing to learn from, and a base model installed in its place
        # would be the switch lying about what it did.
        fresh = meta_label.MetaLabeller(uses_xasset=True)

    # Swapped in whole, so a reader never sees one fit's status with another
    # fit's weights.
    meta_label._instance = fresh
    _last_fit_date = date_str
    _last_toggle = toggle

    if with_x is not None:
        try:
            (recorder or _record_fit)(
                ts=now.timestamp(), n=with_x.status.n_samples,
                auc_base=cmp_base.status.auc_oos, auc_xasset=with_x.status.auc_oos,
                installed="xasset" if fresh is with_x else "base", per_peer=per_peer)
        except Exception as e:                    # noqa: BLE001
            log.warning("[RE-Meta] could not record the fit: %s", e)

    st = fresh.status
    log.info("[RE-Meta] fitted (%s): ready=%s n=%d folds=%d AUC(oos)=%s AUC(in)=%s%s%s",
             "cross-asset" if fresh.uses_xasset else "base",
             st.ready, st.n_samples, st.n_folds,
             None if st.auc_oos is None else round(st.auc_oos, 3),
             None if st.auc_in_sample is None else round(st.auc_in_sample, 3),
             f" refused: {st.refusal}" if st.refusal else "",
             "" if with_x is None else
             f" | same rows without/with peers: {cmp_base.status.auc_oos} / "
             f"{with_x.status.auc_oos} (n={with_x.status.n_samples})")
