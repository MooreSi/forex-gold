"""Approving and rejecting a remote client's licence request from Telegram,
and mirroring the approval into the admin console's Licence Manager."""
from __future__ import annotations

import logging

from backend.src.services.positions._panel_shared import Screen

log = logging.getLogger(__name__)


_REG_DURATION_LABELS = {"6m": "6 Months", "1y": "1 Year", "2y": "2 Years",
                        "3y": "3 Years", "perp": "Perpetual"}

def _resolve_pending_token(short: str):
    """Pending registrations are addressed by their token's own first 8 hex
    chars in callback_data (a full token is 64 hex chars — far too long for
    Telegram's 64-byte cap). Resolve back to the real token here."""
    from backend.src.services.cluster.remote import server as _remote_server
    for tok in _remote_server._pending:
        if tok.startswith(short):
            return tok
    return None

def _record_licence_issued(token: str) -> None:
    """Mirror forex_admin.py's own post-approval step so a Telegram approval
    shows up in the admin console's Licence Manager the same way a WS-console
    approval does. No-ops cleanly if KeyGen's DB callbacks aren't registered
    (e.g. this instance isn't the one running the admin console)."""
    from backend.src.services.cluster.remote import server as _remote_server
    if not _remote_server._kg_insert_fn:
        return
    tok_meta   = _remote_server._allowed_tokens.get(token, {})
    lic_key    = tok_meta.get("licence_key", "")
    machine_id = tok_meta.get("machine_id", "")
    if not (lic_key and machine_id):
        return
    try:
        already = False
        if _remote_server._kg_get_all_fn:
            already = any(r.get("licence_key") == lic_key
                          for r in _remote_server._kg_get_all_fn())
        if already:
            return
        plat = tok_meta.get("platform", "")
        os_str = ("macOS" if plat == "darwin" else
                  "Windows" if "win" in plat.lower() else plat or "Unknown")
        _remote_server._kg_insert_fn({
            "email":           tok_meta.get("email", ""),
            "registration_id": machine_id,
            "sha256":          "",
            "machine_model":   os_str,
            "hostname":        tok_meta.get("hostname", ""),
            "macos_version":   os_str,
            "licence_key":     lic_key,
            "expiry_date":     tok_meta.get("expiry_date", ""),
            "licence_type":    tok_meta.get("subscription_type", ""),
            "notes":           "Auto-issued via Telegram approval",
        })
        import asyncio
        asyncio.create_task(_remote_server._push_licences_to_all_admins())
    except Exception as exc:
        log.warning("[Panel] licence DB insert failed for %s: %s", token[:8], exc)

def _approve_registration(short: str, duration_code: str) -> Screen:
    import asyncio
    from backend.src.services.cluster.remote import server as _remote_server

    token = _resolve_pending_token(short)
    if not token:
        return Screen(toast="Request no longer pending — maybe already handled.", mode="noop")

    pending      = _remote_server._pending.get(token, {})
    sub_type     = _REG_DURATION_LABELS.get(duration_code, "Perpetual")
    display_name = pending.get("nickname") or pending.get("hostname") or token[:8]

    ok = _remote_server.approve_registration(token, display_name, sub_type)
    if not ok:
        return Screen(toast="Approval failed — request may have expired.", mode="noop")

    _record_licence_issued(token)
    asyncio.create_task(_remote_server._push_pending_to_all_admins())
    asyncio.create_task(_remote_server._push_clients_to_all_admins())

    tok_meta = _remote_server._allowed_tokens.get(token, {})
    warn = "" if tok_meta.get("licence_key") else \
        "\n⚠️ Licence key generation failed — check the signing key is registered."
    return Screen(
        text=f"✅ Approved — {display_name}\nSubscription: {sub_type}{warn}",
        keyboard=[],
        toast=f"Approved ({sub_type})",
        mode="edit",
    )

def _reject_registration(short: str) -> Screen:
    from backend.src.services.cluster.remote import server as _remote_server
    import asyncio

    token = _resolve_pending_token(short)
    if not token:
        return Screen(toast="Request no longer pending — maybe already handled.", mode="noop")

    pending = _remote_server._pending.pop(token, None) or {}
    _remote_server._save_pending()
    asyncio.create_task(_remote_server._push_pending_to_all_admins())

    name = pending.get("nickname") or pending.get("hostname") or token[:8]
    return Screen(text=f"❌ Rejected — {name}", keyboard=[], toast="Rejected", mode="edit")
