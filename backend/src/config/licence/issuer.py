"""Which machine is allowed to be the licence issuer (the admin server).

Until 2026-09-12 the answer was two filesystem facts -- a `KeyGen/forex_admin.py`
somewhere findable, and a non-empty `remote/admin_password.hash` -- and both
turned out to be true by accident on a client Mac. `~/Documents` is inside the
iCloud Drive container on the owner's Apple ID, so KeyGen lands on every Mac he
signs into, and a password hash left over from an earlier session never expires.
That machine came back from a restart as a SERVER: `app.startup()` picks the
server OR the client, never both, so it also stopped reporting to the console
that was supposed to be managing it.

The fix is to ask the hardware instead of the filesystem. This adds no
fragility that licensing does not already carry -- `get_fingerprint()` is what
the licence itself is keyed to -- and it needs nothing done on the client
machine, which is the point: the machine that has gone wrong is the one you
cannot reach.

`FOREX_ADMIN_MACHINE_FINGERPRINT` in the environment overrides the constant,
so a replaced or reimaged admin Mac is recoverable without shipping a build.

Lives under `config/licence/` next to `fingerprint.py`, not under
`services/cluster/remote/`, because `config/` sits at the bottom of the import
stack: `licence/guard.py` needs the same answer for the activation screen and
may not reach up into `services/`.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# The owner's Mac mini. Not a secret and not a credential: knowing it grants
# nothing without KeyGen and the admin password, and the repo already carries
# the admin WAN IP (remote/ip_check.py) and server address (remote/tls.py) in
# the same spirit.
ADMIN_MACHINE_FINGERPRINT = "FOREX-349E9267-EBB61E27-539D9A41-3AAC333E"

_ENV_OVERRIDE = "FOREX_ADMIN_MACHINE_FINGERPRINT"


def _read_fingerprint() -> str:
    """This machine's hardware fingerprint. A seam, so a test can say which
    machine it is without owning the hardware."""
    from backend.src.config.licence.fingerprint import get_fingerprint
    return get_fingerprint()


def expected_fingerprint() -> str:
    return os.environ.get(_ENV_OVERRIDE, "").strip() or ADMIN_MACHINE_FINGERPRINT


def is_licence_issuer_machine() -> bool:
    """True only on the machine that issues licences.

    Never raises: a fingerprint that cannot be read comes back from
    `get_fingerprint()` as its own fallback value, which will not match, and
    the machine is treated as a client. That is the safe direction -- a client
    that wrongly refuses a console is a nuisance, a client that wrongly becomes
    a server disappears from the fleet.
    """
    try:
        actual = _read_fingerprint()
    except Exception as exc:          # pragma: no cover -- get_fingerprint catches its own
        log.warning("[Issuer] Could not read this machine's fingerprint: %s", exc)
        return False
    return actual == expected_fingerprint()
