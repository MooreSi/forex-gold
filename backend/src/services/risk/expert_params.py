"""Expert Tunables — the Tier-A behaviour constants, made configurable (M7).

CONFIG_AUDIT.md found ~135 hardcoded numeric constants across the services
and sorted them into three tiers. This module exposes Tier A: the short
list a trader might genuinely want to move, as opposed to the calibration
constants whose interactions are only verified by the test suite.

Storage follows strategy_params.py exactly, because that pattern is
already proven in this codebase: defaults live in code, an override lives
in app_config as JSON, reads merge the override over the defaults so a
newly-added param can never disappear, the cache registers an invalidator
so a demo/live switch does not keep serving the other environment's
values, and unknown keys are dropped rather than stored so a typo cannot
sit in the DB looking like configuration.

Two properties are load-bearing and are asserted by
tests/core/test_expert_params.py:

  1. EVERY DEFAULT IS BYTE-IDENTICAL to the constant it replaces. Nothing
     trades differently until somebody moves a dial. A tunables page that
     silently retunes the system on upgrade would be worse than the
     hardcoded constants it replaces.

  2. Every value is CLAMPED to a declared range. Several of these gate
     order placement — a 0 in the R:R floor would open trades the system
     currently refuses — so the clamp is a safety control, not input
     tidying. The ranges are documented guesses, flagged for review in
     docs/todo/refactor/OPEN_QUESTIONS.md.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from backend.src.db import database as db_module
from backend.src.db.database import _schedule_coro

log = logging.getLogger(__name__)

_CONFIG_KEY = "expert_params"


@dataclass(frozen=True)
class ExpertParam:
    key: str
    label: str
    default: float
    min: float
    max: float
    unit: str
    desc: str
    domain: str
    # int params are stored and returned as ints -- a miss threshold of
    # 2.5 cycles is meaningless, and float creep into a comparison against
    # an integer counter is a real bug source.
    integer: bool = False


# Grouped by domain; the UI renders these generically in this order.
EXPERT_PARAMS: list[ExpertParam] = [
    # ── Risk filters ─────────────────────────────────────────────────────
    ExpertParam(
        key="min_tp1_rr", label="Minimum TP1 reward:risk", default=0.75,
        min=0.10, max=5.0, unit="R", domain="Risk filters",
        desc="A signal whose TP1 is closer than this multiple of its stop "
             "distance is skipped entirely. Raising it trades less often "
             "and more selectively; lowering it accepts thinner setups.",
    ),
    ExpertParam(
        key="rg_min_tp1_rr", label="Risk Governor TP1 R:R floor", default=1.00,
        min=0.10, max=5.0, unit="R", domain="Risk filters",
        desc="The Risk Governor's own reward:risk floor, applied when the "
             "governor is enabled. Independent of the pre-trade filter "
             "above, and normally stricter.",
    ),
    ExpertParam(
        key="rg_max_stop_atr", label="Risk Governor max stop width", default=1.5,
        min=0.25, max=10.0, unit="xATR", domain="Risk filters",
        desc="Rejects scalps whose stop is wider than this multiple of "
             "ATR. Guards against sizing a position off an unusually wide "
             "stop during a volatility spike.",
    ),
    ExpertParam(
        key="max_unprotected_trades", label="Max unprotected same-direction trades",
        default=2, min=1, max=10, unit="trades", domain="Risk filters",
        integer=True,
        desc="Blocks a new trade when this many open trades in the same "
             "direction have not yet reached breakeven. The cap on "
             "correlated exposure to one move going wrong.",
    ),

    # ── Instant Market Entry ─────────────────────────────────────────────
    ExpertParam(
        key="ime_sl_min_pts", label="IME stop — minimum", default=8.0,
        min=1.0, max=100.0, unit="pt", domain="Instant Market Entry",
        desc="Lower clamp on the provisional stop an instant entry opens "
             "with before the follow-up signal supplies the real one.",
    ),
    ExpertParam(
        key="ime_sl_max_pts", label="IME stop — maximum", default=25.0,
        min=1.0, max=200.0, unit="pt", domain="Instant Market Entry",
        desc="Upper clamp on that provisional stop. Caps the loss if a "
             "follow-up never arrives and the timeout closes the trade.",
    ),
    ExpertParam(
        key="ime_sl_atr_mult", label="IME stop — ATR multiplier", default=1.2,
        min=0.1, max=10.0, unit="x", domain="Instant Market Entry",
        desc="The provisional stop is this multiple of ATR, before the "
             "min/max clamps above are applied.",
    ),
    ExpertParam(
        key="ime_followup_timeout_s", label="IME follow-up timeout", default=180,
        min=30, max=3600, unit="s", domain="Instant Market Entry",
        integer=True,
        desc="How long an instant trade waits for the follow-up signal "
             "carrying its real SL/TP before the watchdog assigns them.",
    ),

    # ── Signal handling ──────────────────────────────────────────────────
    ExpertParam(
        key="pending_signal_expiry_s", label="Queued signal expiry", default=120,
        min=15, max=3600, unit="s", domain="Signal handling",
        integer=True,
        desc="A signal queued waiting for price to re-enter its zone is "
             "cancelled after this long. These are scalps; an old zone is "
             "a different setup.",
    ),
    ExpertParam(
        key="max_signal_age_s", label="Maximum signal age", default=240,
        min=30, max=3600, unit="s", domain="Signal handling",
        integer=True,
        desc="Signals older than this at processing time are recorded as "
             "historical and never executed. Raised carelessly, this is "
             "how a backfilled signal fills minutes late at a worse price.",
    ),
    ExpertParam(
        key="duplicate_window_s", label="Duplicate suppression window", default=900,
        min=0, max=86400, unit="s", domain="Signal handling",
        integer=True,
        desc="A near-identical signal arriving inside this window of the "
             "previous one is treated as a repost, not a new trade.",
    ),

    # ── Broker reconciliation ────────────────────────────────────────────
    ExpertParam(
        key="placeholder_no_fill_expiry_s", label="Unfilled placeholder expiry",
        default=86400, min=3600, max=518400, unit="s",
        domain="Broker reconciliation", integer=True,
        desc="How long an EA Template placeholder with no broker position and "
             "no broker deal is kept open before it is written off as never "
             "filled (no P&L -- nothing was ever opened). Below this it is "
             "left alone, because its legs may still be resting as pending "
             "orders. Set too low, a resting order that was about to fill is "
             "closed out from under itself; too high, a dead row keeps "
             "consuming one of your max-open-trades slots. The ceiling is "
             "under the 7-day broker deal-history lookback on purpose: past "
             "that, 'no deal' stops meaning 'never filled' and starts meaning "
             "'too old to see'.",
    ),
    ExpertParam(
        key="mt5_sync_miss_threshold", label="Broker-close miss threshold",
        default=2, min=1, max=20, unit="cycles", domain="Broker reconciliation",
        integer=True,
        desc="How many consecutive reconciliation cycles a ticket must be "
             "absent from MT5 before the app treats the trade as closed. "
             "1 makes a single dropped request look like a close.",
    ),
]

_SPECS: dict[str, ExpertParam] = {p.key: p for p in EXPERT_PARAMS}
_DEFAULTS: dict[str, float] = {p.key: p.default for p in EXPERT_PARAMS}

_CACHE_TTL = 10.0  # seconds -- these change only on user edit
_cache: dict[str, tuple[dict, float]] = {}


def _cache_clear() -> None:
    _cache.clear()


# Keyed on time, not on which database is open: without this a demo/live
# switch would keep serving the other environment's values for up to
# _CACHE_TTL, and these values gate order placement. Same defect
# get_risk_settings() had -- see database.init().
db_module.register_cache_invalidator(_cache_clear)


def spec(key: str) -> Optional[ExpertParam]:
    return _SPECS.get(key)


def specs_by_domain() -> dict[str, list[ExpertParam]]:
    """Catalogue grouped for generic rendering, preserving declared order."""
    grouped: dict[str, list[ExpertParam]] = {}
    for param in EXPERT_PARAMS:
        grouped.setdefault(param.domain, []).append(param)
    return grouped


def defaults() -> dict[str, float]:
    return dict(_DEFAULTS)


def _coerce(param: ExpertParam, value) -> float:
    """Clamp into the safe range, then apply the int rule."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return param.default
    number = max(param.min, min(number, param.max))
    return int(number) if param.integer else number


def all_values() -> dict[str, float]:
    """Live values -- DB override merged over the built-in defaults, so a
    param never disappears if the read fails or the stored override
    predates a newly-added param."""
    now = time.time()
    cached = _cache.get(_CONFIG_KEY)
    if cached is not None and (now - cached[1]) < _CACHE_TTL:
        # A COPY, not the cached dict. Handing out the cache itself let a
        # caller holding a snapshot watch it change underneath them: taking
        # a snapshot and then resetting mutated the snapshot to the
        # defaults, so the value forwarded over sync was whatever the local
        # node happened to do next. Caught by
        # test_expert_params.py::test_the_snapshot_round_trips.
        return dict(cached[0])

    values = defaults()
    raw = db_module.get_app_config(_CONFIG_KEY)
    if raw:
        try:
            override = json.loads(raw)
            for key, value in override.items():
                param = _SPECS.get(key)
                if param is not None:
                    values[key] = _coerce(param, value)
        except Exception as exc:
            log.debug("[ExpertParams] bad override: %s", exc)

    # Defaults are declared, not coerced, above -- run them through the
    # same rule so an int param reads back as an int on a clean install.
    for key, param in _SPECS.items():
        if param.integer:
            values[key] = int(values[key])

    _cache[_CONFIG_KEY] = (values, now)
    return dict(values)


def get(key: str) -> float:
    """Live value for one param. Raises KeyError for an unknown key --
    a typo must not read back as a silent zero."""
    if key not in _SPECS:
        raise KeyError(f"Unknown expert param: {key}")
    return all_values()[key]


_applying_sync = False  # re-entrancy guard -- see strategy_params.set_strategy_params


def catalogue() -> dict:
    """{domain: [row, ...]} where each row carries the spec plus the live value.

    Both value and default are included because every row offers a reset
    control and has to know whether it is currently overridden. Rows are plain
    dicts so the page never needs to import this module to read a field.

    This lived in settings/controller.py as a nested comprehension. Building a
    view model is service work: the shape of a row is this module's business,
    and a controller that assembles it is a controller that can assemble a
    different one.
    """
    values = all_values()
    return {
        domain: [
            {
                "key":     param.key,
                "label":   param.label,
                "value":   values[param.key],
                "default": param.default,
                "min":     param.min,
                "max":     param.max,
                "unit":    param.unit,
                "desc":    param.desc,
                "integer": param.integer,
            }
            for param in params
        ]
        for domain, params in specs_by_domain().items()
    }


def set_params(values: dict, _from_sync: bool = False) -> dict:
    """Merge `values` over the current overrides. Unknown keys are dropped
    rather than stored: a typo'd key would otherwise sit in the DB looking
    like configuration while nothing ever read it."""
    global _applying_sync
    merged = all_values()
    for key, value in values.items():
        param = _SPECS.get(key)
        if param is None:
            log.debug("[ExpertParams] dropping unknown key %r", key)
            continue
        merged[key] = _coerce(param, value)

    db_module.set_app_config(_CONFIG_KEY, json.dumps(merged))
    _cache.pop(_CONFIG_KEY, None)

    if not _from_sync and not _applying_sync:
        _applying_sync = True
        try:
            _forward_over_sync()
        finally:
            _applying_sync = False
    return merged


def reset(key: str) -> dict:
    """Revert one param to its built-in default."""
    if key not in _SPECS:
        raise KeyError(f"Unknown expert param: {key}")
    return set_params({key: _SPECS[key].default})


def reset_all() -> dict:
    return set_params(defaults())


def snapshot() -> dict:
    """The full live set, for the node-sync channel."""
    return all_values()


def apply_snapshot(values: dict) -> dict:
    """Apply a snapshot that arrived over sync, without re-forwarding it."""
    return set_params(values, _from_sync=True)


def _forward_over_sync() -> None:
    """Send the full snapshot to the paired node, whichever role this
    process has. No-op and near-zero cost when sync is not configured.
    Mirrors strategy_params._forward_strategy_params_over_sync."""
    try:
        from backend.src.services.cluster.sync import client as _sync_cli_mod
        cli = _sync_cli_mod.get_instance()
        if cli is not None and hasattr(cli, "propose_expert_params"):
            _schedule_coro(cli.propose_expert_params(snapshot()))
            return
    except Exception as exc:
        log.debug("[Sync] expert params forward (client) failed: %s", exc)

    try:
        from backend.src.services.cluster.sync import server as _sync_srv_mod
        srv = _sync_srv_mod.get_instance()
        if srv is not None and hasattr(srv, "broadcast_expert_params"):
            _schedule_coro(srv.broadcast_expert_params())
    except Exception as exc:
        log.debug("[Sync] expert params forward (server) failed: %s", exc)
