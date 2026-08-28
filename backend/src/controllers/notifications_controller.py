"""Email the UI sends: the settings page's test message, and the ORB report.

Forwards to backend.src.services.notifications.email_service unchanged. No
decisions here -- the point is that the pages reach it through a controller, so
a change to the email service's signature has one caller to fix rather than
five.

send_email actually sends. It is the same call the pages were making directly
before this file existed, routed rather than altered, but it is worth naming:
nothing on this module is inert.
"""
from __future__ import annotations

from backend.src.services.notifications import email_service as _email

__all__ = [
    "send_email",
    "build_orb_html",
    "build_orb_chart_image",
    "ORB_CHART_CID",
]

# The Content-ID the ORB chart is attached under, so the HTML can reference it
# with <img src="cid:...">. Re-exported because the page builds that tag.
ORB_CHART_CID = _email._ORB_CHART_CID


def send_email(*args, **kwargs):
    """Send a message. Outbound -- see the module docstring."""
    return _email.send_email(*args, **kwargs)


def build_orb_html(*args, **kwargs):
    return _email.build_orb_html(*args, **kwargs)


def build_orb_chart_image(*args, **kwargs):
    return _email.build_orb_chart_image(*args, **kwargs)
