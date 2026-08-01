"""
Email service — transport only after the M2 split: SMTP / Resend / Mailjet
delivery. The HTML builders live in email_html.py and are re-exported here
verbatim so callers keep addressing email_service.build_*_html.
Uses Python stdlib smtplib so no extra dependencies required.
"""

import asyncio
import logging
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from backend.src.services.notifications.email_html import (  # noqa: F401
    _ORB_CHART_CID,
    build_daily_html,
    build_orb_chart_image,
    build_orb_html,
    build_weekly_html,
)

log = logging.getLogger(__name__)

# ── Send ──────────────────────────────────────────────────────────────────────

def _make_ssl_context() -> ssl.SSLContext:
    """
    Build an SSL context that works on macOS Python.org installs.
    The framework Python ships without system CA certs installed; certifi
    provides a complete, up-to-date bundle that resolves the common
    'certificate verify failed: unable to get local issuer certificate' error.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    # Fallback: macOS system keychain bundle
    system_pem = "/etc/ssl/cert.pem"
    if ssl.os.path.exists(system_pem):
        return ssl.create_default_context(cafile=system_pem)
    return ssl.create_default_context()


def _send_sync(
    host: str, port: int, user: str, password: str,
    use_tls: bool, from_addr: str, to_addr: str,
    msg: MIMEMultipart,
) -> None:
    context = _make_ssl_context()
    if use_tls and port == 465:
        # Implicit TLS (SSL from the start)
        with smtplib.SMTP_SSL(host, port, context=context, timeout=20) as smtp:
            if user and password:
                smtp.login(user, password)
            smtp.sendmail(from_addr, [to_addr], msg.as_string())
    else:
        # Explicit TLS (STARTTLS upgrade on port 587 or 25)
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.ehlo()
            if use_tls:
                smtp.starttls(context=context)
                smtp.ehlo()
            if user and password:
                smtp.login(user, password)
            smtp.sendmail(from_addr, [to_addr], msg.as_string())


# Personal email domains that Resend cannot use as a verified sender domain.
# When the from_addr belongs to one of these, fall back to onboarding@resend.dev.
_PERSONAL_EMAIL_DOMAINS = {
    "gmail.com", "outlook.com", "hotmail.com", "live.com", "live.co.uk",
    "msn.com", "yahoo.com", "yahoo.co.uk", "icloud.com", "me.com",
    "mac.com", "protonmail.com", "proton.me", "aol.com", "zoho.com",
}


def _resend_sender(from_addr: str) -> str:
    """Return a Resend-compatible from address.

    Personal email domains (gmail, outlook, etc.) cannot be verified as Resend
    sender domains, so we fall back to the shared onboarding@resend.dev address.
    """
    if not from_addr:
        return "onboarding@resend.dev"
    domain = from_addr.split("@")[-1].lower() if "@" in from_addr else ""
    if domain in _PERSONAL_EMAIL_DOMAINS:
        return "onboarding@resend.dev"
    return from_addr


async def _send_via_resend(
    subject: str, html_body: str, cfg: dict,
    image_bytes: Optional[bytes] = None, image_cid: str = "", image_filename: str = "chart.png",
) -> tuple[bool, str]:
    """
    Send via Resend API (resend.com).
    Free tier: 3,000 emails/month.  No SMTP, no app passwords.
    From address: use  onboarding@resend.dev  (Resend shared domain — no setup needed)
    OR your own domain once verified in the Resend dashboard.
    """
    import base64
    import httpx as _httpx

    api_key   = (cfg.get("resend_api_key") or "").strip()
    to_addr   = (cfg.get("to_addr") or "").strip()
    from_addr = _resend_sender((cfg.get("from_addr") or "").strip())

    if not api_key:
        return False, "Resend API key is required"
    if not to_addr:
        return False, "Recipient address (To) is required"

    from_field = f"FOREX Trader <{from_addr}>"

    payload = {
        "from":    from_field,
        "to":      [to_addr],
        "subject": subject,
        "html":    html_body,
    }
    if image_bytes and image_cid:
        # Resend's documented inline-image schema: attachments[].content_id
        # referenced from the HTML via <img src="cid:...">.
        payload["attachments"] = [{
            "filename":     image_filename,
            "content":      base64.b64encode(image_bytes).decode(),
            "content_type": "image/png",
            "content_id":   image_cid,
        }]
    try:
        async with _httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if r.status_code in (200, 201):
            log.info("Resend sent: %r → %s", subject, to_addr)
            return True, ""
        # If domain validation fails despite our check, retry with the shared sender.
        if r.status_code == 403 and "domain" in r.text.lower() and from_addr != "onboarding@resend.dev":
            log.warning("Resend domain not verified (%s), retrying with onboarding@resend.dev", from_addr)
            payload["from"] = "FOREX Trader <onboarding@resend.dev>"
            async with _httpx.AsyncClient(timeout=20) as client:
                r = await client.post(
                    "https://api.resend.com/emails",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            if r.status_code in (200, 201):
                log.info("Resend sent (fallback sender): %r → %s", subject, to_addr)
                return True, ""
        return False, f"Resend HTTP {r.status_code}: {r.text[:300]}"
    except Exception as e:
        return False, f"Resend error: {e}"


async def _send_via_mailjet(
    subject: str, html_body: str, cfg: dict,
    image_bytes: Optional[bytes] = None, image_cid: str = "", image_filename: str = "chart.png",
) -> tuple[bool, str]:
    """Send via Mailjet REST API — no SMTP, no port issues, 1500 free/month."""
    import httpx as _httpx

    api_key    = (cfg.get("mailjet_api_key") or "").strip()
    secret_key = (cfg.get("mailjet_secret_key") or "").strip()
    from_addr  = (cfg.get("from_addr") or cfg.get("smtp_user") or "").strip()
    to_addr    = (cfg.get("to_addr") or "").strip()

    if not api_key or not secret_key:
        return False, "Mailjet API key and Secret key are required"
    if not from_addr or not to_addr:
        return False, "From and To addresses are required"
    if image_bytes and image_cid:
        # Mailjet's inline-attachment schema isn't verified against their docs
        # here (unlike Resend's) since neither node currently uses Mailjet for
        # delivery — sending the text without the embedded chart rather than
        # guess at the field names.
        log.info("Mailjet: inline chart image not attached (schema not verified) — sending text only")

    payload = {
        "Messages": [{
            "From":    {"Email": from_addr, "Name": "FOREX Trader"},
            "To":      [{"Email": to_addr,   "Name": to_addr}],
            "Subject": subject,
            "HTMLPart": html_body,
        }]
    }
    try:
        async with _httpx.AsyncClient(timeout=20) as client:
            r = await client.post(
                "https://api.mailjet.com/v3.1/send",
                json=payload,
                auth=(api_key, secret_key),
            )
        if r.status_code == 200:
            result = r.json()
            sent = result.get("Messages", [{}])[0]
            if sent.get("Status") == "success":
                log.info("Mailjet sent: %r → %s", subject, to_addr)
                return True, ""
            return False, f"Mailjet status: {sent.get('Status')} — {sent.get('Errors', '')}"
        return False, f"Mailjet HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"Mailjet error: {e}"


async def send_email(
    subject: str,
    html_body: str,
    cfg: Optional[dict] = None,
    *,
    image_bytes: Optional[bytes] = None,
    image_cid: str = "",
    image_filename: str = "chart.png",
) -> tuple[bool, str]:
    """
    Send an HTML email. Returns (success, error_message).

    Routing:
      - Explicit cfg (from test buttons) routes by which keys are present.
      - Scheduled sends (cfg=None) route by the saved send_provider setting.

    image_bytes/image_cid: optional inline image (e.g. a PNG chart) referenced
    from html_body via <img src="cid:{image_cid}">. Supported on Resend and
    SMTP; silently omitted (text still sends) on Mailjet — see
    _send_via_mailjet's docstring for why.
    """
    from backend.src.db import database as db_module
    if cfg is None:
        cfg = db_module.get_email_config()

    # Tag every outgoing email with which node sent it (Local Mac vs Remote
    # VPS) — same flag the UI's Local/Remote toggle and run.py's browser-skip
    # logic already use, so it always matches what the header shows.
    node_label = "Remote (VPS)" if db_module.get_app_config("sync_server_enabled") == "1" else "Local"
    subject = f"[{node_label}] {subject}"
    html_body = (
        f'<div style="background:#111827;color:#9ca3af;font-size:11px;'
        f'font-family:Arial,sans-serif;padding:6px 12px;text-align:center;">'
        f'Sent from: {node_label} node</div>'
    ) + html_body

    # When a test button passes an explicit cfg_snap it will only contain the
    # keys for that provider, so the checks below route correctly. For scheduled
    # sends the full db config is used and send_provider steers the choice.
    send_provider = (cfg.get("send_provider") or "").strip()

    # ── Resend path ───────────────────────────────────────────────────────────
    has_resend = bool((cfg.get("resend_api_key") or "").strip())
    if has_resend and (not send_provider or send_provider == "resend"):
        return await _send_via_resend(subject, html_body, cfg, image_bytes, image_cid, image_filename)

    # ── Mailjet path ──────────────────────────────────────────────────────────
    has_mailjet = bool((cfg.get("mailjet_api_key") or "").strip())
    if has_mailjet and send_provider == "mailjet":
        return await _send_via_mailjet(subject, html_body, cfg, image_bytes, image_cid, image_filename)

    # ── SMTP path ─────────────────────────────────────────────────────────────
    host     = (cfg.get("smtp_host") or "").strip()
    to_addr  = (cfg.get("to_addr") or "").strip()
    if not host:
        if has_resend:
            # send_provider is set to SMTP but SMTP host is missing — fall back to Resend
            log.warning("send_provider=%r but no SMTP host configured; falling back to Resend", send_provider)
            return await _send_via_resend(subject, html_body, cfg, image_bytes, image_cid, image_filename)
        return False, "SMTP host not configured"
    if not to_addr:
        return False, "Recipient address not configured"

    port      = int(cfg.get("smtp_port") or 587)
    user      = (cfg.get("smtp_user") or "").strip()
    password  = (cfg.get("smtp_password") or "").strip()
    from_addr = (cfg.get("from_addr") or user or "").strip()
    use_tls   = bool(cfg.get("use_tls", 1))

    alt_part = MIMEMultipart("alternative")
    alt_part.attach(MIMEText(html_body, "html", "utf-8"))

    if image_bytes and image_cid:
        from email.mime.image import MIMEImage
        msg = MIMEMultipart("related")
        msg.attach(alt_part)
        img_part = MIMEImage(image_bytes, "png")
        img_part.add_header("Content-ID", f"<{image_cid}>")
        img_part.add_header("Content-Disposition", "inline", filename=image_filename)
        msg.attach(img_part)
    else:
        msg = alt_part

    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: _send_sync(host, port, user, password, use_tls, from_addr, to_addr, msg),
        )
        log.info("Email sent (SMTP): %r → %s", subject, to_addr)
        return True, ""
    except Exception as e:
        log.error("SMTP send failed: %s", e)
        return False, str(e)
