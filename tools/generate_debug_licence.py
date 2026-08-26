"""Request a licence for this machine, and install one the admin has signed.

**This no longer mints a key, and that is the point.** It used to call
`keygen.generate_licence_key()`, which worked because the signing secret shipped
inside the code -- so anyone with a copy of the repo could issue themselves a
licence. Upstream `7251656` closed that: signing moved to Ed25519 with the
private key held only in KeyGen, outside this repository, and `keygen.py` was
deleted. Simon confirmed the replacement in Q007 #1
(docs/simon-handover/007-remaining-approvals.md): licences come from his admin
console.

So this tool now does the two honest halves of that:

    python -m tools.generate_debug_licence            # show this machine's id
    python -m tools.generate_debug_licence --install KEY --expiry perpetual

The first prints the fingerprint to send to Simon (or to match against the
pending registration his console shows). The second installs the key he signs,
verifying it against the shipped public key first -- so a typo fails here rather
than at the next app start.

`guard.enforce()` is untouched, and the rule that debug mode never bypasses the
licence check still holds.
"""
from __future__ import annotations

import argparse

from backend.src.config.licence import store
from backend.src.config.licence.fingerprint import get_fingerprint
from backend.src.config.licence.verify import verify_licence_key


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Show this machine's licence fingerprint, or install a signed key.")
    ap.add_argument("--install", metavar="KEY",
                    help="install a licence key signed by the admin console")
    ap.add_argument("--expiry", default="perpetual",
                    help="expiry the key was signed for (YYYY-MM-DD, or 'perpetual')")
    ap.add_argument("--email", default="", help="optional, recorded alongside the licence")
    args = ap.parse_args()

    machine_id = get_fingerprint()

    if not args.install:
        print(f"machine_id : {machine_id}")
        print(f"expiry     : {args.expiry}")
        print()
        print("This machine cannot sign its own licence -- the private key lives in")
        print("KeyGen, outside this repo (Q007 #1). To get one:")
        print()
        print("  1. Start the app. It shows the activation screen and sends a")
        print("     registration request to the admin server.")
        print("  2. Simon approves it from the admin console, or from the inline")
        print("     Approve buttons in Telegram, choosing a duration.")
        print("  3. The signed key is pushed back and installed automatically.")
        print()
        print("If the app cannot reach the admin server, have Simon sign this")
        print("machine_id by hand and install it here:")
        print()
        print("  python -m tools.generate_debug_licence --install <KEY> --expiry perpetual")
        return 0

    key = args.install.strip()
    if not verify_licence_key(machine_id, args.expiry, key):
        print("REFUSED: that key is not a valid signature for this machine.")
        print(f"  machine_id : {machine_id}")
        print(f"  expiry     : {args.expiry}")
        print()
        print("Check the expiry matches exactly what it was signed for -- the")
        print("signature covers machine_id, expiry and version together, so a")
        print("mismatched expiry fails just like a wrong key.")
        return 1

    store.save({
        "machine_id":   machine_id,
        "expiry_date":  args.expiry,
        "licence_key":  key,
        "licence_type": "Perpetual" if args.expiry == "perpetual" else "Timed",
        "email":        args.email,
    })
    print(f"Verified and installed to {store.STORE_PATH}.")
    print("guard.enforce() will now pass on this machine.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
