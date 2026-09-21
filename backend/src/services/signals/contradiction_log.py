"""Record what each contradiction policy WOULD have done. Decide nothing.

Stage 1 of contradiction handling between the Telegram channels and the
internal signal generators. **Nothing here changes a trade.** It answers the
question the design could not: how often do the sources actually disagree on
this account, and what would each policy have cost?

It sits on the order path, which fixes three of its properties. All three
are `decision_log`'s, and they are not restated here for symmetry -- they
are the reason a research log is allowed on this path at all.

**It is off unless asked for.** One column, `tg_contradiction_log_enabled`,
on the Parsing page, default 0. With it off nothing here runs: no bus read,
no bus write, no clock arithmetic beyond the toggle check. The bus WRITE is
part of that promise and not an afterthought -- `signal_bus` is read by a
live suppression gate, so a feature that kept writing rows to it while
"off" would be off in name only.

**It never raises.** Every public function swallows everything. Signal
processing must not be able to fail because a study could not be recorded.

**An unavailable fact abstains.** Fill state is not on the bus, so
`freshest wins` records NULL rather than an answer -- see
`contradiction.Active.is_pending`. Putting fill state on the bus is the
stage-2 item, and it needs a write on both engines' execution paths, which
is not a change this stage is allowed to make.

WHY THE BUS WRITE LIVES HERE AND NOT AT THE CALL SITES
------------------------------------------------------
A Telegram signal has never been on the bus. Putting it there is what makes
channel-vs-channel visible at all, and it has to happen at the same instant
the observation is taken, from the same toggle, or the two disagree about
what was live. The engines are the other way round: they already write the
bus unconditionally as live behaviour, so they call `observe` alone and
their write stays exactly where it was.

`engine` (the bus's own column, and what every reader excludes on) is set
to the SOURCE NAME for a Telegram row, not to the literal "telegram". A
shared value there would make every channel exclude every other channel,
which is precisely the contradiction this exists to see.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from backend.src.db import database as db_module
from backend.src.services.signals import contradiction as _c
from backend.src.services.signals import contradiction_log_repo as _repo

log = logging.getLogger(__name__)

SETTING_KEY = "tg_contradiction_log_enabled"

# How long a signal stays capable of contradicting the next one. Matches the
# shipped policies' own window rather than either engine's TTL: the bus
# carries 180s (reversal) and 21600s (breakout) side by side, and a study
# that inherited whichever engine wrote last would be measuring the TTLs.
OBSERVE_WINDOW_S = 600.0

# How long a Telegram row stays on the bus. Longer than the widest policy
# window so a policy is never limited by the row expiring underneath it, and
# short enough that the 30-minute prune keeps the table small.
TELEGRAM_TTL_S = 1800.0

# The lens used to decide what goes in `opposing_json`: every pair, the full
# window. A policy's own narrower scope is applied when that policy is
# resolved -- the recorded set is the evidence, not one policy's view of it.
_REFERENCE = _c.Policy("reference", _c.MODE_FIRST_WINS,
                       window_secs=OBSERVE_WINDOW_S, pairs=_c.ALL_PAIRS)


def enabled(rs: dict) -> bool:
    """True when the operator has switched the study on. Defensive: a caller
    on the order path must not be handed an exception by a settings read."""
    try:
        return bool(rs.get(SETTING_KEY, 0))
    except Exception:
        return False


def _account_env() -> Optional[str]:
    """demo or live. A column rather than a separate database -- see
    contradiction_log_repo's header for why the study is one file."""
    try:
        from backend.src import config as _config
        return str(_config.get("account_env", "demo"))
    except Exception:
        return None


def _active_entries(source_name: str, symbol: str) -> list[_c.Active]:
    """Everything on the bus that this source might be contradicting.

    `is_pending` is None for every entry and that is not a placeholder: the
    bus records whether a signal is still OPEN, which is a different fact
    from whether it has FILLED. Inventing either value here would hand the
    one policy that reads it a number nobody measured.
    """
    rows = db_module.get_concurrent_signals(
        exclude_engine=source_name,
        window_seconds=OBSERVE_WINDOW_S,
        kinds=(_c.KIND_ENGINE, _c.KIND_TELEGRAM),
        symbol=symbol or None,
    )
    return [
        _c.Active(
            source_kind=str(r.get("source_kind") or _c.KIND_ENGINE),
            source_name=str(r.get("source_name") or r.get("engine") or ""),
            direction=str(r.get("direction") or ""),
            symbol=str(r.get("symbol") or ""),
            created_at=float(r.get("created_at") or 0.0),
            is_pending=None,
            signal_ref=str(r.get("signal_id") or ""),
        )
        for r in rows
    ]


def observe(rs: dict, *, source_kind: str, source_name: str, direction: str,
            symbol: str = "", candidate_ref: str = "",
            at: Optional[float] = None) -> None:
    """Record this signal and every policy's verdict on it. Never raises."""
    if not enabled(rs):
        return
    try:
        now = float(at if at is not None else time.time())
        candidate = _c.Candidate(source_kind=source_kind, source_name=source_name,
                                 direction=direction, symbol=symbol, at=now)
        active = _active_entries(source_name, symbol)
        opposing = _c.opposing_entries(candidate, active, _REFERENCE, now)

        row_id = _repo.insert_observation({
            "observed_at": now,
            "account_env": _account_env(),
            "candidate_ref": candidate_ref or f"{source_name}:{now:.0f}",
            "source_kind": source_kind,
            "source_name": source_name,
            "direction": (direction or "").upper(),
            "symbol": symbol,
            "opposing_count": len(opposing),
            "opposing": [
                {"source_kind": a.source_kind, "source_name": a.source_name,
                 "direction": a.direction, "created_at": a.created_at}
                for a in opposing
            ],
        })
        if row_id is None:
            return  # already recorded -- an ordinary rescan, not a failure

        for policy in _c.POLICIES:
            verdict = _c.resolve(candidate, active, policy, now=now)
            _repo.insert_verdict(
                row_id, policy.name,
                None if verdict.action == _c.UNKNOWN else verdict.action,
                verdict.reason, verdict.lot_mult, now,
            )
    except Exception as exc:
        log.debug("[Contradiction] observe failed: %s", exc)


def note_signal(rs: dict, *, source_kind: str, source_name: str,
                direction: str, symbol: str = "", candidate_ref: str = "",
                at: Optional[float] = None) -> None:
    """Observe this signal, then put it on the bus so the next one sees it.

    For Telegram only. Observe first: a signal must not be able to
    contradict itself, and while `resolve` already excludes its own source
    by name, the ordering is what makes that a belt rather than the only
    strap.

    Never raises, including the bus write -- `write_signal_bus` already
    swallows and returns 0.
    """
    if not enabled(rs):
        return
    observe(rs, source_kind=source_kind, source_name=source_name,
            direction=direction, symbol=symbol, candidate_ref=candidate_ref, at=at)
    try:
        db_module.write_signal_bus(
            source_name, direction, ttl_seconds=TELEGRAM_TTL_S,
            symbol=symbol or None, source_kind=source_kind,
            source_name=source_name,
        )
    except Exception as exc:
        log.debug("[Contradiction] bus write failed: %s", exc)


def observe_engine_signal(source_name: str, direction: str, signal_id,
                          rs: Optional[dict] = None) -> None:
    """`observe` for an internal engine, at the point it writes the bus.

    A named helper rather than the same six lines in both engines: those two
    files sit one line under the 800-line gate, and a study is not a good
    reason to spend a shrink-only budget twice.

    The engines' own `write_signal_bus` calls are deliberately NOT made from
    here. They are live behaviour that the other engine's suppression gate
    reads, they carry engine-specific TTLs (180s and 21600s), and they must
    keep running when this study is off.

    `rs` is read here when the caller does not hold it -- unlike the
    Telegram path, an engine reaches this a handful of times a day, not once
    per scanned message.
    """
    try:
        if rs is None:
            rs = db_module.get_risk_settings() or {}
        from backend.src.utils import models as _models
        observe(rs, source_kind=_c.KIND_ENGINE, source_name=source_name,
                direction=direction, symbol=_models.SYMBOL,
                candidate_ref=f"{source_name}:{signal_id}")
    except Exception as exc:
        log.debug("[Contradiction] engine observe failed: %s", exc)


def report() -> list[dict]:
    """Per policy: how often each verdict came up, over one account env."""
    try:
        return _repo.report(_account_env())
    except Exception as exc:
        log.debug("[Contradiction] report failed: %s", exc)
        return []
