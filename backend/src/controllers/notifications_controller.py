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
    "build_orb_report",
    "ORB_CHART_CID",
]


def _get_engine():
    """The live runtime, or None before startup has built one.

    Deferred: importing the composition root at module scope would have every
    importer of this controller pull the whole application graph.
    """
    from backend.src.app import get_engine
    return get_engine()


async def build_orb_report():
    """The opening-range report the scheduled email renders.

    Here because the rest of this email's path already is: the settings page
    called build_orb_chart_image, build_orb_html and send_email through this
    controller, then reached around it to `backend.src.app` for the one piece
    that needed the runtime's broker bridge. That import was one of the last
    two sites keeping the frontend contract off zero.

    Returns None when there is no runtime yet -- a headless node, or a button
    pressed before startup finishes. A failure INSIDE the report (no bridge, no
    candles) propagates: the page says something different for each, and
    flattening both to None would make a broken bridge look like a quiet
    morning.
    """
    engine = _get_engine()
    if engine is None:
        return None
    return await engine.build_orb_report()

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
