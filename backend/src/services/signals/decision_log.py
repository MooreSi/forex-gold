"""Record what the app decided about a Telegram signal, and why (stage 0).

docs/todo/signal-validation/010. This is a research log on the order path,
which makes three of its properties non-negotiable.

**It is off unless asked for.** One column, `tg_decision_log_enabled`, shown
on the Parsing page. With it off nothing here runs -- no calendar read, no
database write, no clock arithmetic beyond the toggle check.

**It never raises.** `core_signal_snapshot` polls the signal table rather
than hooking the parser precisely so a research log cannot break signal
processing. This one cannot poll -- the thing it records is a decision, not
a row that exists afterwards -- so it buys the same guarantee the only other
way available: every public function here swallows everything.

**Every fact is gathered before any gate returns.** `inline_facts` is called
once, up front, and the same dict is carried to whichever exit the decision
takes. `reversal_engine_live_execute.py` states the rule and the reason:
recording at each early return would log "would take" for a variant whose
later gates were never run, which reads as an endorsement it never gave.

The facts are computed with the gates forced ON, ignoring their live
toggles, because a shadow of a gate that is already on answers nothing. They
are also all free: a clock read, the calendar `check_news_blackout` has
already warmed, and numbers the caller is holding. Nothing is fetched. The
measured IME budget is 269 ms end to end with 256 ms of it the broker POST,
and this has to stay in the noise.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from backend.src.services.risk import event_tiers as _events
from backend.src.services.risk import session_liquidity as _liquidity
from backend.src.services.signals import decision_log_repo as _repo

log = logging.getLogger(__name__)

SETTING_KEY = "tg_decision_log_enabled"


def enabled(rs: dict) -> bool:
    """True when the operator has switched the log on. Defensive: a caller
    on the order path must not be handed an exception by a settings read."""
    try:
        return bool(rs.get(SETTING_KEY, 0))
    except Exception:
        return False


def _events_now(now_ts: float) -> list:
    """Calendar events shaped for `event_tiers.check`.

    Named and separated so a test can break it, because the failure that
    matters is the feed being down, not the arithmetic being wrong.
    """
    from backend.src.utils.news_calendar import get_events
    return [dict(ev, mins_until=(float(ev.get("ts", 0)) - now_ts) / 60.0)
            for ev in get_events()]


def inline_facts(rs: dict, now_ts: float, tick: Any,
                 events: Optional[list] = None) -> dict:
    """The shadow fact set for one decision. Never raises.

    A fact that could not be established is **None**, never False. None
    means "not known"; False would be read as "checked, and fine", and a
    variant that stood aside on a fact it never had would report a refusal
    rate that says nothing about the variant. Same rule as `shadow.decide`.

    Each fact is caught separately so one broken input does not cost the
    others -- `run_snapshot_cycle` isolates its three cadences for the same
    reason.
    """
    facts: dict = {"liquidity_blocked": None, "event_blocked": None,
                   "spread_points": None}

    try:
        ok, _reason = _liquidity.check(float(now_ts), _liquidity.Config())
        facts["liquidity_blocked"] = not ok
    except Exception:
        log.debug("[DecisionLog] liquidity fact unavailable", exc_info=True)

    try:
        evs = _events_now(float(now_ts)) if events is None else events
        ok, _reason = _events.check(evs, _events.Config())
        facts["event_blocked"] = not ok
    except Exception:
        log.debug("[DecisionLog] event fact unavailable", exc_info=True)

    try:
        if tick is not None:
            facts["spread_points"] = float(tick.spread_points)
    except Exception:
        log.debug("[DecisionLog] spread fact unavailable", exc_info=True)

    return facts


def _account_env() -> Optional[str]:
    """demo or live. A column rather than a separate database -- see
    decision_log_repo's header for why the corpus is one file."""
    try:
        from backend.src import config as _config
        return str(_config.get("account_env", "demo"))
    except Exception:
        return None


def record(*, rs: dict, tg_id: str, path: str, channel_name: str,
           direction: str, executed: bool, skip_reason: str,
           strategy: Optional[str], parsed: Optional[dict], tick: Any,
           trade_id: Optional[str], facts: Optional[dict] = None,
           now_ts: Optional[float] = None) -> Optional[int]:
    """One decision. Returns the row id, or None when the log is off or
    anything at all went wrong.

    `parsed` is optional because IME decides on a bare direction with no
    levels at all, and that is the path most in need of measuring.
    """
    try:
        if not enabled(rs):
            return None
        import time as _time
        now = _time.time() if now_ts is None else now_ts
        p = parsed or {}
        f = facts if facts is not None else inline_facts(rs, now, tick)

        def _num(key):
            try:
                v = p.get(key)
                return None if v is None else float(v)
            except (TypeError, ValueError):
                return None

        def _flag(key):
            v = f.get(key)
            return None if v is None else int(bool(v))

        return _repo.insert_decision({
            "tg_message_id": str(tg_id or ""),
            "path": path,
            "channel_name": channel_name,
            "direction": (direction or "").upper() or None,
            "decided_at": now,
            "executed": 1 if executed else 0,
            "skip_reason": skip_reason or "",
            "strategy": strategy,
            "entry_low": _num("entry_low"), "entry_high": _num("entry_high"),
            "stop_loss": _num("stop_loss"), "tp1": _num("tp1"),
            "bid": getattr(tick, "bid", None),
            "ask": getattr(tick, "ask", None),
            "spread_points": f.get("spread_points"),
            "price": getattr(tick, "ask", None) if (direction or "").upper() == "BUY"
                     else getattr(tick, "bid", None),
            "session": _session_for(),
            "trade_id": trade_id,
            "account_env": _account_env(),
            "liquidity_blocked": _flag("liquidity_blocked"),
            "event_blocked": _flag("event_blocked"),
        })
    except Exception:
        log.debug("[DecisionLog] record failed", exc_info=True)
        return None


def summary() -> dict:
    """What the log has seen, without needing a single outcome.

    The variant report can only score decisions whose trade has closed, so
    it is silent for weeks and silent forever about the signals that were
    declined. This is the other half, and the half that is useful on day
    one: how many decisions, how many each path executed and declined, and
    what actually did the declining.

    Never raises. A page renders it, and a card that throws takes a live
    trading screen down with it.
    """
    empty = {"total": 0, "executed": 0, "blocked": 0, "resolved": 0,
             "awaiting_outcome": 0, "observed": 0, "reconstructed": 0,
             "by_path": [], "top_reasons": []}
    try:
        counts = _repo.decision_counts() or {}
        total = int(counts.get("total") or 0)
        executed = int(counts.get("executed") or 0)
        resolved = int(counts.get("resolved") or 0)
        reconstructed = int(counts.get("reconstructed") or 0)
        return {
            "total": total,
            "executed": executed,
            "blocked": total - executed,
            "resolved": resolved,
            # Executions whose trade has not closed yet. Named so the gap
            # between this and the variant report reads as "still open"
            # rather than "lost somewhere".
            "awaiting_outcome": max(executed - resolved, 0),
            "observed": total - reconstructed,
            "reconstructed": reconstructed,
            "by_path": [
                {"path": r.get("path"),
                 "executed": int(r.get("executed") or 0),
                 "blocked": int(r.get("blocked") or 0)}
                for r in _repo.counts_by_path()
            ],
            "top_reasons": [
                {"reason": r.get("reason"), "n": int(r.get("n") or 0)}
                for r in _repo.counts_by_reason()
            ],
        }
    except Exception:
        log.debug("[DecisionLog] summary failed", exc_info=True)
        return empty


def _session_for() -> Optional[str]:
    """The trading session, from the one definition of it.

    Reads the clock rather than taking `decided_at`, because that is what
    `sessions.get_session()` does and a second, timestamp-taking copy of
    those boundaries is exactly the drift 057 catalogued. The two are the
    same moment on the live path, which is the only path that writes here.
    """
    try:
        from backend.src.services.market import sessions as _sessions
        return _sessions.get_session()
    except Exception:
        return None
