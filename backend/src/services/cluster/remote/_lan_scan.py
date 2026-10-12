"""Where the remote client's LAN-discovery scan is allowed to run.

The scan probes every host on the machine's local /24 for the admin server's
port. On a home LAN those hosts are the owner's own devices. On a VPS the
adapter carries a public address, so that /24 belongs to the hosting provider's
other customers: on 2026-10-12 the owner's VPS made about 3,000 connection
attempts to them and was cut off the network (tests/remote/
test_lan_scan_stays_on_private_networks.py).
"""
import ipaddress


def is_scannable(local_ip: str) -> bool:
    """True only for a private or loopback address.

    A public address, an empty string or anything unparseable returns False: a
    machine that cannot tell which network it is on does not sweep one.
    """
    try:
        return ipaddress.ip_address(local_ip).is_private
    except ValueError:
        return False
