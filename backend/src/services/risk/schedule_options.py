"""The choices the Trading Schedule screen offers, and the Trading Markets
toggles that sit above it.

The schedule grid itself lives in `schedule.py`. This module supplies the
things the screen needs to *render* the grid: which markets are enabled and
which session is live, the list of strategies and EA templates a window can
force a source onto, and the Telegram channels a window can gate.

It exists because the React port had none of it. The NiceGUI page assembled
these inline from four different controllers, which is exactly the merging a
controller may not do -- so the assembly is here, in a service, and the
controller forwards to it.

**The live session is read through `is_session_allowed`, not recomputed.**
The NiceGUI page derived its own label from `datetime.utcnow().hour` and the
same three settings. That is a second opinion about the one question the gate
already answers, and the two can disagree -- a badge reading "London" while
the gate refuses the trade is worse than no badge.

Nothing here places an order or reaches a broker.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Every collaborator is reached through a thin module-level wrapper that
# imports inside the call. Two reasons, and the first is not style:
#
# `risk_settings_repo` imports `db.database`, which imports `risk_settings_repo`
# back. At module scope that cycle is fatal -- importing this file from a cold
# process dies with "cannot import name '_RS_CACHE_TTL' from partially
# initialized module". Every other module in this package already defers the
# same import for the same reason (see clock.py's `_rs`).
#
# It also gives a test one name to replace per collaborator, instead of
# reaching through four modules to patch where each really lives.
def _get_risk_settings() -> dict:
    from backend.src.services.risk.risk_settings_repo import get_risk_settings
    return get_risk_settings()


def _update_risk_settings(fields: dict):
    from backend.src.services.risk.risk_settings_repo import update_risk_settings
    return update_risk_settings(fields)


def _is_session_allowed(rs=None):
    from backend.src.services.risk.risk_settings_repo import is_session_allowed
    return is_session_allowed(rs)


# The settings key behind each button on the card. The UI speaks in market
# names; the database has spoken in these since before the card existed.
_MARKET_KEYS = {
    "asia": "session_asia_enabled",
    "london": "session_london_enabled",
    "new_york": "session_ny_enabled",
}


def _list_ea_templates() -> list[dict]:
    from backend.src.services.broker.ea_templates import list_ea_templates
    return list_ea_templates()


def _override_for_template(name: str) -> str:
    from backend.src.services.broker.ea_templates import override_for_template
    return override_for_template(name)


def _channel_names() -> list[str]:
    from backend.src.services.channels.performance import get_telegram_channel_names
    return get_telegram_channel_names()


def markets() -> dict:
    """The three Trading Markets toggles, and which session is live now.

    Every toggle defaults to enabled: a settings row written before these
    keys existed must keep trading exactly as it did, not go dark.
    """
    rs = _get_risk_settings() or {}
    allowed, session = _is_session_allowed(rs)
    return {
        "asia": bool(rs.get("session_asia_enabled", 1)),
        "london": bool(rs.get("session_london_enabled", 1)),
        "new_york": bool(rs.get("session_ny_enabled", 1)),
        "session": session,
        "allowed_now": bool(allowed),
    }


def set_markets(fields: dict) -> None:
    """Switch one or more markets. Keys are market names, values are bools.

    An unknown name is refused rather than ignored: a typo that silently
    wrote nothing looks exactly like a toggle that does not work, and this
    one decides whether automated orders are placed at all.
    """
    if not fields:
        return
    unknown = [k for k in fields if k not in _MARKET_KEYS]
    if unknown:
        raise ValueError(
            f"unknown trading market(s): {', '.join(sorted(unknown))} -- "
            f"expected one of {', '.join(sorted(_MARKET_KEYS))}"
        )
    _update_risk_settings({
        _MARKET_KEYS[name]: 1 if bool(value) else 0
        for name, value in fields.items()
    })


def override_options() -> list[dict]:
    """The per-source Override dropdown: [{value, label}], in display order.

    "No Override" is first and its value is the empty string, which is what
    a block stores when nothing is forced. "Auto (AI-managed)" is second by
    intent rather than accident -- it is the option most likely to be
    wanted, which is where the NiceGUI page put it.

    A failure to read the EA templates costs the template entries and
    nothing else. An operator who cannot pick a strategy because the
    template table is missing has lost more than they needed to.
    """
    from backend.src.utils.models import STRATEGY_NAMES

    options = [
        {"value": "", "label": "— No Override —"},
        {"value": "auto", "label": "Auto (AI-managed)"},
    ]
    options.extend({"value": key, "label": label}
                   for key, label in STRATEGY_NAMES.items())
    try:
        for template in _list_ea_templates():
            name = template.get("name")
            if not name:
                continue
            options.append({"value": _override_for_template(name),
                            "label": f"Template: {name}"})
    except Exception as exc:
        log.warning("[Schedule] EA templates unavailable for the Override "
                    "dropdown: %s", exc)
    return options


def channel_names() -> list[str]:
    """The Telegram channels a window can allow or block individually.

    None rather than an exception: the rest of the screen -- the windows,
    the targets, both engines -- is still configurable without them.
    """
    try:
        return list(_channel_names())
    except Exception as exc:
        log.warning("[Schedule] Telegram channels unavailable: %s", exc)
        return []


def screen_extras() -> dict:
    """The three additions to the schedule screen, in one guarded read.

    `/api/schedule/state` is the screen's single read: the grid, the enabled
    flag and the daily target ARE the screen. These three are additions to it,
    and an addition that cannot be read must cost itself rather than the page.
    A 500 here would leave the operator unable to see or edit their trading
    windows because a dropdown's contents were unavailable.

    **`markets` degrades to None, not to "all enabled".** Every other fallback
    here is cheerful and safely so -- an empty dropdown is obviously empty. A
    markets card defaulting to every session on would tell the operator that
    trading is allowed in sessions that may be switched off, which is a
    statement about whether real orders are being placed. The screen shows
    nothing rather than something wrong.
    """
    try:
        market_state = markets()
    except Exception as exc:
        log.warning("[Schedule] Trading Markets unreadable: %s", exc)
        market_state = None

    try:
        options = override_options()
    except Exception as exc:
        log.warning("[Schedule] override options unreadable: %s", exc)
        options = []

    return {
        "markets": market_state,
        "override_options": options,
        # `channel_names` guards itself and already returns [] on failure.
        "channels": channel_names(),
    }
