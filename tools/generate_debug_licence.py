"""Generate and install a GENUINE perpetual licence for this machine.

This is NOT a bypass: it writes a real licence key that the untouched
`guard.enforce()` HMAC-verifies exactly like any other. It exists so a developer
without an admin-issued key can run the app locally (esp. in debug mode). The
key is bound to this machine's fingerprint and useless anywhere else.

Usage:
    python -m tools.generate_debug_licence
    python -m tools.generate_debug_licence --show   # just print, don't install
"""
from __future__ import annotations

import argparse

from backend.src.config.licence import store
from backend.src.config.licence.fingerprint import get_fingerprint
from backend.src.config.licence.keygen import generate_licence_key, verify_licence_key


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--show", action="store_true", help="print the key without installing")
    args = ap.parse_args()

    machine_id = get_fingerprint()
    expiry = "perpetual"
    key = generate_licence_key(machine_id, expiry)

    assert verify_licence_key(machine_id, expiry, key), "generated key failed self-verification"

    print(f"machine_id : {machine_id}")
    print(f"expiry     : {expiry}")
    print(f"licence_key: {key}")

    if args.show:
        return 0

    store.save({
        "machine_id": machine_id,
        "expiry_date": expiry,
        "licence_key": key,
        "licence_type": "Perpetual",
        "email": "debug@localhost",
    })
    print(f"\nInstalled to {store.STORE_PATH}. guard.enforce() will now pass on this machine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
