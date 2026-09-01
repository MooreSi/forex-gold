"""Create the admin channel's private CA, and issue server certificates from it.

Run by the owner, offline. Nothing in the app calls this.

    # once, on a machine that is NOT the VPS
    python -m tools.make_remote_ca init --dir ~/forex-admin-ca

    # whenever the admin server needs a certificate
    python -m tools.make_remote_ca issue --dir ~/forex-admin-ca \
        --address 217.155.25.160 --out ./server-cert

Then:
  * copy `server-cert/server_cert.pem` and `server_cert/server_key.pem` to the
    VPS's USER_DATA_DIR/remote/,
  * copy `ca_cert.pem` into the app source so it ships with the build,
  * and keep `ca_key.pem` offline. Anyone holding it can mint a certificate
    this app trusts.

The CA key must never live on the VPS. The VPS holds only the certificate the
CA signed for it: a stolen server key lets an attacker impersonate that one
server until it is reissued, and does not let them mint anything new.

See docs/todo/bugs/014 for why this exists and backend/src/services/cluster/
remote/ca.py for why it is a private CA rather than Let's Encrypt.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from backend.src.services.cluster.remote import ca as remote_ca


def _init(args) -> int:
    directory = Path(args.dir).expanduser()
    try:
        cert, key = remote_ca.init_ca(directory)
    except FileExistsError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    print(f"authority created in {directory}")
    print(f"  {cert.name}  -> copy this into the app so it ships with the build")
    print(f"  {key.name}   -> KEEP OFFLINE. Anyone holding it can mint a")
    print( "                  certificate the app will trust.")
    return 0


def _issue(args) -> int:
    directory = Path(args.dir).expanduser()
    ca_cert = directory / remote_ca.CA_CERT_NAME
    ca_key = directory / remote_ca.CA_KEY_NAME
    if not ca_cert.exists() or not ca_key.exists():
        print(f"no authority in {directory} — run `init` first", file=sys.stderr)
        return 1
    out = Path(args.out).expanduser()
    cert, key = remote_ca.issue_server_cert(ca_cert, ca_key, out, args.address)
    print(f"issued for {', '.join(args.address)}")
    print(f"  {cert}")
    print(f"  {key}")
    print("copy both to the admin server's USER_DATA_DIR/remote/ and restart it")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="create the certificate authority (once)")
    p_init.add_argument("--dir", required=True,
                        help="where to write ca_cert.pem and ca_key.pem")
    p_init.set_defaults(func=_init)

    p_issue = sub.add_parser("issue", help="sign a server certificate")
    p_issue.add_argument("--dir", required=True, help="the authority's directory")
    p_issue.add_argument("--address", required=True, action="append",
                         help="IP or hostname the server answers on; repeatable")
    p_issue.add_argument("--out", required=True, help="where to write the certificate")
    p_issue.set_defaults(func=_issue)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
