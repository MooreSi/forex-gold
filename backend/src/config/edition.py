"""Which gates this build puts in front of the app.

There are two, and this module is the only place that decides whether either
one stands:

* **the licence key** -- `config/licence/guard.enforce()`, the Ed25519
  activation screen that blocks startup until a machine is registered;
* **the dashboard password** -- `api/auth.AuthGate`, the login the operator
  sees before the trading controls.

Both are OFF here. The owner opened this repository to the community on
2026-09-22, and a clone that demands an activation code from an issuer only he
runs is a clone nobody can start.

**Nothing was removed to do that.** `config/licence/` is complete and still
fully tested, `api/auth.py` is untouched, and the admin console, the issuer and
the remote self-heal push all still work. Putting a gate back is one constant
here -- which is the entire reason this module exists rather than the call
sites simply being deleted.

Two ways to turn one on:

* set the constant below and ship that build;
* set the environment variable for a single run, without editing a tracked
  file, so a private build can live in a checkout of this same repo.

The override is **one-directional on purpose**: it can require a gate, never
waive one. An environment variable that could switch a licence check off would
travel with every build ever made from this tree, including one sold with the
gate compiled in -- that is the licence bypass this project's rules forbid, and
it is not what this is.
"""
from __future__ import annotations

import os

__all__ = [
    "LICENCE_REQUIRED", "AUTHENTICATION_REQUIRED",
    "LICENCE_ENV_VAR", "LOGIN_ENV_VAR",
    "licence_required", "authentication_required",
]

# ── The switches ─────────────────────────────────────────────────────────────

#: Require a signed licence key before the app starts.
LICENCE_REQUIRED = False

#: Require a dashboard password before the trading controls are reachable.
AUTHENTICATION_REQUIRED = False

LICENCE_ENV_VAR = "FOREX_REQUIRE_LICENCE"
LOGIN_ENV_VAR = "FOREX_REQUIRE_LOGIN"

_TRUE = {"1", "true", "yes", "on"}


def _env_requires(name: str) -> bool:
    """Whether `name` is set to something that means yes.

    Anything else -- unset, empty, "0", a typo -- means no. A misspelt value
    must not be read as consent to turn a gate on any more than off.
    """
    return os.environ.get(name, "").strip().lower() in _TRUE


def licence_required() -> bool:
    """Whether `run.py` runs the licence guard before starting the app."""
    return bool(LICENCE_REQUIRED) or _env_requires(LICENCE_ENV_VAR)


def authentication_required() -> bool:
    """Whether the dashboard asks for a password.

    Read in two places, which must agree: `run.py` decides whether to install
    `AuthGate`, and `/api/auth/session` tells the React app whether to render
    the login page. A server with no gate and a client still drawing the form
    is a password prompt with nothing behind it.
    """
    return bool(AUTHENTICATION_REQUIRED) or _env_requires(LOGIN_ENV_VAR)
