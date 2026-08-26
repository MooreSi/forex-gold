"""Account numbers and email addresses must not reach the logs.

WHY (Q005 #1, docs/simon-handover/005-fact-finding.md)
-----------------------------------------------------
The diagnostics feature uploads roughly 3,000 raw log lines to the admin
server. Until 2026-08-26 the MT5 connect line wrote the account number, the
broker server and the balance into every one of them, and the email sender
wrote each recipient address -- both confirmed from Simon's own captured logs,
not from reading the code.

These pin the masking. No test here connects to anything.
"""
from __future__ import annotations

import re

from backend.src.utils.os_utils import mask_account, mask_email


def test_mask_account_keeps_only_the_last_three_digits():
    assert mask_account(12345678) == "*****678"
    assert "12345" not in mask_account(12345678)


def test_mask_account_hides_a_short_number_entirely():
    """A 3-digit account would otherwise be shown in full by a tail rule."""
    assert mask_account(99) == "***"
    assert mask_account(123) == "***"


def test_mask_account_handles_nothing_at_all():
    assert mask_account(None) == "***"
    assert mask_account("") == "***"


def test_mask_email_keeps_the_domain_and_drops_the_person():
    assert mask_email("simon.moore@outlook.com") == "***@outlook.com"
    assert "simon" not in mask_email("simon.moore@outlook.com")


def test_mask_email_passes_through_empty_and_malformed():
    assert mask_email("") == ""
    assert mask_email("not-an-address") == "***"


def test_the_bridge_connect_line_no_longer_formats_the_raw_login():
    """The specific regression: mt5_bridge.py's connect log.

    Reads the source rather than running the bridge, which needs MetaTrader5
    and a terminal. The assertion is about what the format call is handed.
    """
    src = open("mt5_bridge.py", encoding="utf-8").read()
    connect = re.search(r'log\.info\(\s*\n\s*"Connected to MT5\..*?\)', src, re.S)
    assert connect, "the connect log line moved -- re-point this test"
    block = connect.group(0)
    assert "info.login" not in block, "the raw login is being formatted into the log again"
    assert "info.balance" not in block, "the balance is back in the log line"
    assert "_masked" in block


def test_every_email_send_masks_its_recipient():
    """A new send path that logs `to_addr` raw would slip past the others."""
    src = open("backend/src/services/notifications/email_service.py", encoding="utf-8").read()
    raw = re.findall(r'log\.info\([^)]*→ %s"[^)]*,\s*to_addr\s*\)', src)
    assert raw == [], f"recipient logged unmasked at {len(raw)} site(s)"
