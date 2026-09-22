"""Renaming an EA template, and repointing everything that named it.

**A template's name is a foreign key nobody declared.** Every strategy
override in this app is the string `template:<name>` -- in the trading
schedule (per window, and again per Telegram channel inside each window), on a
channel's own assignment, in the AI's per-channel recommendation and in the
global risk settings. SQLite knows nothing about any of it.

That matters because `ea_templates.template_for_channel` resolves those routes
IN ORDER and falls through to the next one when a name does not resolve. A
rename that left the references behind would raise nothing and show nothing:
the channel would simply start trading under a different strategy than the one
its screen says. This module is here so that cannot happen.

**History is not a reference.** A closed trade recorded as
`template:Old Name` was placed under that name, and the Analysis tab's
attribution is built from that exact string. Rewriting it would falsify the
record to make a label tidy. `vantage_simulated_trades`, `consolidated_trades`,
`execution_quality`, `vantage_pending_orders` and `vantage_signals.notes` all
hold the name for that reason and are deliberately untouched.

Nothing here places, closes or modifies an order. Repointing an override is
meant to be a no-op in trading terms -- the same template, under a new name --
and the report it returns is how the operator can check that it was.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

__all__ = ["rename", "references"]

# The keys inside one schedule window that can hold a strategy override. Named
# rather than discovered: a window also holds times, targets and flags, and a
# sweep that rewrote every string value in the blob would corrupt them.
_WINDOW_OVERRIDE_KEYS = (
    "strategy_override",
    "reversal_engine_override",
    "breakout_engine_override",
)


def _swap(value: Any, old: str, new: str) -> tuple[Any, bool]:
    """`new` when `value` is exactly `old`, else `value` unchanged.

    Whole-value equality, never a substring replace: "Old Name" and
    "Old Name v2" are two different templates, and a `str.replace` here would
    silently repoint the second one at the first one's new name.
    """
    return (new, True) if value == old else (value, False)


def _sweep_schedule(old: str, new: str, *, apply: bool) -> int:
    """Repoint the trading schedule's windows. Returns how many it touched."""
    from backend.src.services.risk import schedule as _sched

    schedule = _sched.get_trading_schedule() or {}
    hits = 0
    for _day, windows in schedule.items():
        if not isinstance(windows, list):
            continue
        for window in windows:
            if not isinstance(window, dict):
                continue
            for key in _WINDOW_OVERRIDE_KEYS:
                window[key], changed = _swap(window.get(key), old, new)
                hits += changed
            # Per-channel overrides live a level deeper in the same blob. A
            # sweep that walked only the window's own keys would leave every
            # one of them pointing at a name that no longer exists.
            channels = window.get("telegram_channels")
            if isinstance(channels, dict):
                for _name, cfg in channels.items():
                    if not isinstance(cfg, dict):
                        continue
                    cfg["strategy_override"], changed = _swap(
                        cfg.get("strategy_override"), old, new)
                    hits += changed
    if hits and apply:
        _sched.set_trading_schedule(schedule)
    return hits


def _sweep_channels(old: str, new: str, *, apply: bool) -> tuple[int, int]:
    """Repoint channel assignments and AI recommendations.

    The recommendation counts. With no explicit assignment
    `template_for_channel` reads it next, so a stale one does not fail -- it
    hands the channel the global strategy instead.
    """
    from backend.src.services.channels import repo as _ch
    from backend.src.services.channels import strategy_lookup_repo as _lookup

    assigned = recommended = 0
    for row in _lookup.fetch_sources_using_strategy(old):
        source = row["source"]
        if row["assigned"]:
            if apply:
                _ch.set_channel_strategy_override(source, new, row["auto"])
            assigned += 1
        if row["recommended"]:
            if apply:
                rec = _ch.get_channel_strategy_rec(source) or {}
                _ch.set_channel_strategy_rec(
                    source, new, rec.get("reasoning") or "",
                    float(rec.get("confidence") or 0.0))
            recommended += 1
    return assigned, recommended


def _sweep_risk_settings(old: str, new: str, *, apply: bool) -> int:
    """Repoint the global strategy and the one the panel displays."""
    from backend.src.db import database as _db

    rs = _db.get_risk_settings() or {}
    updates = {}
    for key in ("trade_strategy", "display_strategy_id"):
        if rs.get(key) == old:
            updates[key] = new
    if updates and apply:
        _db.update_risk_settings(updates)
    return len(updates)


def _repoint(old_override: str, new_override: str, *, apply: bool) -> dict:
    schedule = _sweep_schedule(old_override, new_override, apply=apply)
    assigned, recommended = _sweep_channels(old_override, new_override, apply=apply)
    risk = _sweep_risk_settings(old_override, new_override, apply=apply)
    return {
        "schedule": schedule,
        "channel_assignments": assigned,
        "ai_recommendations": recommended,
        "risk_settings": risk,
    }


def references(name: str) -> dict:
    """Where `name` is named in LIVE configuration, without changing anything.

    So a panel can warn before a rename rather than after it, and so the
    counts a rename reports can be checked against a dry run.
    """
    from backend.src.services.broker import ea_templates as _et
    override = _et.override_for_template(name)
    return _repoint(override, override, apply=False)


def rename(old: str, new: str) -> dict:
    """Rename a template and repoint every live reference to it.

    Copy, repoint, then delete -- in that order, so a failure part-way leaves
    the template readable under one name or the other rather than under
    neither. The rows are written through `save_ea_template`, which preserves
    the original `created_at`.
    """
    from backend.src.services.broker import ea_templates as _et

    old = (old or "").strip()
    new = (new or "").strip()
    if not new:
        raise ValueError("A template name is required.")
    existing = _et.get_ea_template(old)
    if existing is None:
        raise ValueError(f"No EA template called {old!r}.")
    if new == old:
        # The state asked for already holds. Returning early rather than
        # falling through matters: the delete at the end would otherwise
        # remove the template that was just written under the same name.
        return {"template": existing, "repointed": _repoint(
            _et.override_for_template(old), _et.override_for_template(old),
            apply=False)}
    if _et.get_ea_template(new) is not None:
        raise ValueError(
            f"There is already an EA template called {new!r}. Renaming onto "
            f"it would overwrite a template that cannot be recovered.")

    fields = {k: v for k, v in existing.items()
              if k not in ("name", "created_at", "updated_at")}
    _et.save_ea_template(new, fields)
    # The created_at the template has always had. `save_ea_template` stamps a
    # new one for a row it has not seen before, and a template that claims to
    # have been created today is a template whose history has been lost.
    _carry_created_at(old, new)

    repointed = _repoint(_et.override_for_template(old),
                         _et.override_for_template(new), apply=True)
    _et.delete_ea_template(old)
    log.info("[EATemplates] renamed %r -> %r; repointed %s", old, new, repointed)
    return {"template": _et.get_ea_template(new), "repointed": repointed}


def _carry_created_at(old: str, new: str) -> None:
    from backend.src.services.broker import repo as _repo
    row = _repo.fetch_ea_template(old)
    if row is None:
        return
    from backend.src.db import database as _db
    created = _db.row_to_dict(row).get("created_at")
    if created is not None:
        _repo.set_ea_template_created_at(new, float(created))
