"""
External IP detection and admin machine verification.

The admin machine is identified by having external IP 217.155.25.160.
This is checked asynchronously at server startup and at admin panel login.
Result is cached for 5 minutes so repeated login attempts don't fire HTTP calls.
"""

import asyncio
import logging
import time
from typing import Optional

log = logging.getLogger(__name__)

ADMIN_EXTERNAL_IP = "217.155.25.160"

# Cache: (ip_str, fetched_at)
_ip_cache: tuple[str, float] = ("", 0.0)
_CACHE_TTL = 300  # 5 minutes

# Services tried in order; first success wins
_IP_SERVICES = [
    "https://api.ipify.org",
    "https://checkip.amazonaws.com",
    "https://api4.my-ip.io/ip",
]


async def get_external_ip() -> str:
    """Return this machine's external (WAN) IP address, or '' on failure."""
    global _ip_cache
    cached_ip, fetched_at = _ip_cache
    if cached_ip and (time.monotonic() - fetched_at) < _CACHE_TTL:
        return cached_ip

    import httpx
    for svc in _IP_SERVICES:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(svc)
                ip = r.text.strip()
                if ip:
                    _ip_cache = (ip, time.monotonic())
                    log.debug("[IPCheck] External IP: %s (via %s)", ip, svc)
                    return ip
        except Exception as exc:
            log.debug("[IPCheck] %s failed: %s", svc, exc)

    log.warning("[IPCheck] Could not determine external IP from any service")
    return ""


async def is_admin_machine() -> bool:
    """Return True if this machine's external IP is the configured admin IP."""
    ip = await get_external_ip()
    return ip == ADMIN_EXTERNAL_IP


def invalidate_cache() -> None:
    global _ip_cache
    _ip_cache = ("", 0.0)


def get_machine_uuid() -> str:
    """Return the macOS IOKit Platform UUID, or '' on non-Mac or on failure.

    This UUID is tied to the physical Mac hardware and does not change across
    reboots, OS reinstalls, or hostname changes — suitable for locking remote
    admin access to a specific machine without relying on hostname or IP.
    """
    import sys
    if sys.platform != "darwin":
        return ""
    try:
        import subprocess
        out = subprocess.check_output(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            text=True, timeout=5, stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            if '"IOPlatformUUID"' in line:
                # Line format:  "IOPlatformUUID" = "XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX"
                parts = line.split('"')
                if len(parts) >= 4:
                    return parts[-2]
    except Exception:
        pass
    return ""
