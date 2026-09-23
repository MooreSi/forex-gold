"""Admitting an install of the open build without asking the owner first.

Owner, 2026-09-23: an install from GitHub sent him a Telegram asking him to
authorise its licence key, after he had asked for that requirement to go. The
licence gate itself was already off (`config/edition.py`, 2026-09-22). The
admin server was not told: every unknown client still filed a registration
request, and every request still arrived as Approve/Reject buttons.

A client that says it runs the open build (`licence_required: False` in its
registration) is now admitted straight into the Remote Clients list, and the
owner gets one notification with no buttons.

**Admission is not a licence.** Nothing is signed here, and the record carries
no licence key and no machine id. The second matters as much as the first:
`server.resign_all_licences` runs on every console start and signs a key for
every approved client that has a machine id, so storing one would issue this
client a real licence the next time the console opened.

**Only the open build is admitted.** A client that does not say
`licence_required: False` -- the boolean, not a string -- goes through
approval exactly as before. That covers every licensed build and every build
older than this change. A registration can only claim what the owner has
already made free; it cannot license anything.

Here rather than in `server.py` because that file is at its shrink-only line
ceiling. Both of the server's intake paths call `admit_open` in the condition
of the branch that would otherwise queue the request; the queueing code itself
stays in `server.py`, where `test_registration_carries_the_machine_id` checks
the two paths have not drifted apart. The server's state is read through the module at call time, never
copied: `_load_tokens` rebinds those globals, and a reference taken at import
would go on writing to a dict nobody reads (`/split-file`'s warning).
"""
from __future__ import annotations

import asyncio
import logging
import time

log = logging.getLogger(__name__)

__all__ = ["admit_open"]

#: What the console shows in the subscription column for an admitted client.
OPEN_SUBSCRIPTION = "Open source"


def admit_open(token: str, msg: dict, ip: str) -> bool:
    """Admit `token` if its registration says it runs the open build.

    Returns True when admitted, and the caller then does nothing else with the
    registration. False leaves everything as it was, and the caller queues it
    for approval as it always has.
    """
    from backend.src.services.cluster.remote import server as rs

    if not token or msg.get("licence_required") is not False:
        return False
    if token in rs._allowed_tokens or token in rs._revoked_tokens:
        # Approved already (and possibly paid for), or revoked by the owner.
        # Neither is this function's to change.
        return False

    hostname = msg.get("hostname", "?")
    nickname = msg.get("nickname", "")
    rs._allowed_tokens[token] = {
        "name":              nickname or hostname or token[:8],
        "registered_at":     time.time(),
        "email":             msg.get("email", ""),
        "nickname":          nickname,
        "platform":          msg.get("platform", "unknown"),
        "hostname":          hostname,
        "version":           msg.get("version", ""),
        # No machine_id and no licence_key: see the module docstring.
        "subscription_type": OPEN_SUBSCRIPTION,
        "expiry_date":       "perpetual",
        "licence_key":       "",
        "admitted":          "open build",
    }
    rs._pending.pop(token, None)
    rs._auth_failures.pop(ip, None)
    rs._save_tokens()
    rs._save_pending()
    log.info("[RemoteServer] Admitted %s (%s) — open build, no approval needed",
             hostname, ip)

    asyncio.create_task(rs._push_clients_to_all_admins())
    asyncio.create_task(rs._push_pending_to_all_admins())
    asyncio.create_task(_notify_install(msg, ip))
    return True


def install_message(msg: dict, ip: str) -> str:
    """The owner's notification. Says what happened; asks for nothing."""
    return (
        "New install\n"
        f"Name: {msg.get('nickname') or '—'}\n"
        f"Email: {msg.get('email') or '—'}\n"
        f"Hostname: {msg.get('hostname') or '—'}\n"
        f"Platform: {msg.get('platform') or '—'}\n"
        f"Version: {msg.get('version') or '—'}\n"
        f"IP: {ip or '—'}\n"
        "Added to Remote Clients. No approval needed."
    )


async def _notify_install(msg: dict, ip: str) -> None:
    try:
        from backend.src.services.telegram import alerts as telegram_alerts
        await telegram_alerts.send_message(install_message(msg, ip))
    except Exception as exc:
        log.warning("[RemoteServer] New-install Telegram notify failed: %s", exc)

