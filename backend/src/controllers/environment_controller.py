"""Demo or live: which account the whole application is pointed at.

Its own controller rather than two more operations on `settings_controller`.
That file is the settings page AND the app shell's configuration API and was
over its 200-line ceiling; this is one coherent thing, and a reader looking for
"what can point this app at a live account" should find a file named for it.

`switch_environment_db` used to live next door and re-pointed the database on
its own. That is the MIDDLE of four steps that have to happen together, and
offering it alone is how an app ends up half-switched -- reading one account's
history while sending orders to the other. The sequence, and why its order is
the safety property, is in `services/broker/environment.py`.
"""
from __future__ import annotations

from backend.src.services.broker import environment as _env

__all__ = ["describe_environments", "switch_environment"]


def describe_environments() -> dict:
    """Which account is active, and whether each one can be switched to."""
    return _env.describe()


def switch_environment(*args, **kwargs) -> dict:
    """Point the app at the demo or the live account. Does NOT restart."""
    return _env.switch(*args, **kwargs)
