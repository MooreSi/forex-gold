"""Shared Telethon plumbing for the TelegramReader split (M2
file-size pass): the optional telethon import, auth-state constants, slot
count and small helpers. reader.py re-exports the public names so existing
importers (frontend/pages/telegram.py) keep working unchanged.
"""


import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

from backend.src.db import database as db_module
from backend.src.services.telegram import repo as telegram_repo

log = logging.getLogger(__name__)

try:
    from telethon import TelegramClient, events
    from telethon.errors import (
        ApiIdInvalidError,
        FloodWaitError,
        PhoneCodeExpiredError,
        PhoneCodeInvalidError,
        PhoneNumberInvalidError,
        PasswordHashInvalidError,
        SessionPasswordNeededError,
    )
    from telethon.network import ConnectionTcpAbridged
    from telethon.tl.types import Channel, User
    _TELETHON_AVAILABLE = True
except ImportError:
    _TELETHON_AVAILABLE = False
    log.warning("Telethon not installed — Telegram reader will not function")

# Auth states
AUTH_DISCONNECTED  = "disconnected"
AUTH_AWAITING_CODE = "awaiting_code"
AUTH_AWAITING_2FA  = "awaiting_2fa"
AUTH_CONNECTED     = "connected"
AUTH_RECONNECTING  = "reconnecting"
AUTH_FAILED        = "failed"

_NUM_SLOTS = 3


def _safe_phone_log(phone: Optional[str]) -> str:
    if not phone:
        return "***"
    digits = re.sub(r"\D", "", phone)
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"


def _media_type(msg_obj) -> str:
    if not hasattr(msg_obj, "media") or msg_obj.media is None:
        return "none"
    cls = type(msg_obj.media).__name__.lower()
    if "photo"    in cls: return "photo"
    if "document" in cls: return "document"
    if "video"    in cls: return "video"
    if "audio"    in cls: return "audio"
    return "unknown"


