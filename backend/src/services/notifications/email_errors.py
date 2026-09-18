"""Turning an SMTP exception string into something the operator can act on.

A test send that fails prints whatever the mail server said, and what mail
servers say is famously useless: `535 5.7.139 Authentication unsuccessful` is
Microsoft's way of saying "SMTP AUTH is switched off on your Outlook account",
which nobody would guess and which has a five-click fix.

This lived inside the NiceGUI email settings page until 2026-09-18. It is a
decision about what the operator is told, so it belongs in a service rather
than in a router or a browser: the same failure reaches the daily-report
scheduler and the self-healer, and a translation that only exists in the
dashboard helps nobody there.

The raw error is always appended, truncated. A friendly message that HIDES the
original is worse than no translation at all — it makes an unrecognised
failure unreportable.
"""
from __future__ import annotations

__all__ = ["friendly"]

_RAW_LIMIT = 120


def friendly(raw: str) -> str:
    """An actionable message for a failed send, with the original attached."""
    if not raw:
        return "Failed: the mail server gave no reason."

    r = raw.lower()
    tail = f"\n\nOriginal error: {raw[:_RAW_LIMIT]}"

    # Microsoft 535 — basic auth disabled. Specific before generic: this is a
    # 535 too, and the generic advice ("wrong username or password") sends the
    # operator to reset a password that was never the problem.
    if "535" in r and ("basic authentication" in r
                       or "authentication unsuccessful" in r
                       or "5.7.139" in r):
        return (
            "Microsoft rejected the login: SMTP AUTH (basic authentication) is "
            "disabled on this account.\n\n"
            "To fix it:\n"
            "  1. Open outlook.live.com and sign in\n"
            "  2. Settings (gear) > View all Outlook settings\n"
            "  3. Mail > Sync email\n"
            "  4. Turn 'Authenticated SMTP' ON and Save\n"
            "  5. Come back here and send the test again\n\n"
            "This is a Microsoft account setting, not a problem with this app."
            + tail
        )

    if "535" in r and "gmail" in r:
        return (
            "Gmail rejected the login. Use an App Password rather than your "
            "normal password, and make sure 2-Step Verification is on for the "
            "Google account." + tail
        )

    if "535" in r:
        return (
            "Authentication failed (535) - wrong username or password. If the "
            "account has two-step verification you must use an App Password, "
            "not the account password." + tail
        )

    if "connection refused" in r or "timed out" in r or "network" in r:
        return (
            "Could not reach the mail server. Check the SMTP host and port, and "
            "that outbound connections on that port are not blocked." + tail
        )

    if "certificate" in r or "ssl" in r:
        return (
            "SSL/TLS error. Try turning STARTTLS on, or use port 587 "
            "(STARTTLS) instead of 465." + tail
        )

    return f"Failed: {raw}"
