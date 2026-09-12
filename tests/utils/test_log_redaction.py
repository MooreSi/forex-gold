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


# ── The bot token, found 2026-09-12 ──────────────────────────────────────────
#
# The two masks above were added for the two things Q005 #1 named. A third was
# on the same pages all along: httpx logs the full request URL at INFO on every
# Telegram poll, and a bot token lives in the path.
#
#     GET https://api.telegram.org/bot<id>:<secret>/getUpdates?... "HTTP/1.1 200 OK"
#
# 7,245 lines of the live log carried one on 2026-09-12, and 166 of the last
# 3,000 -- which is exactly the slice `_build_diagnostics` uploads verbatim as
# `log_raw`. The token is full control of the bot: read what it can see, post
# as it. The FILTERED view was already safe by accident, because `_DIAG_NOISY`
# drops anything containing "HTTP/1." and that is every httpx line.

from backend.src.utils.os_utils import scrub_log_secrets  # noqa: E402

_TOKEN_LINE = (
    "2026-09-12 00:00:02,530 INFO httpx — HTTP Request: GET "
    "https://api.telegram.org/bot8821057003:AAF_fakefake_notarealtoken/"
    'getUpdates?offset=446307700&timeout=10 "HTTP/1.1 200 OK"'
)


def test_the_secret_half_of_a_bot_token_is_removed():
    assert "AAF_fakefake_notarealtoken" not in scrub_log_secrets(_TOKEN_LINE)


def test_the_bot_id_survives_so_the_line_still_says_which_bot():
    """Support needs to know WHICH bot; it does not need to be able to use it.
    The numeric id is public in any Telegram username lookup."""
    assert "8821057003" in scrub_log_secrets(_TOKEN_LINE)


def test_the_rest_of_the_line_is_untouched():
    out = scrub_log_secrets(_TOKEN_LINE)
    assert "getUpdates" in out
    assert "2026-09-12 00:00:02,530 INFO httpx" in out
    assert '"HTTP/1.1 200 OK"' in out


def test_every_occurrence_goes_not_only_the_first():
    out = scrub_log_secrets(_TOKEN_LINE + "\n" + _TOKEN_LINE)
    assert "AAF_fakefake_notarealtoken" not in out
    assert out.count("8821057003") == 2


def test_the_token_is_caught_whatever_the_api_method_is():
    """The secret sits between `/bot` and the next slash, whether the call is
    getUpdates, sendMessage or deleteWebhook."""
    assert "SECRETSECRET" not in scrub_log_secrets(
        "POST https://api.telegram.org/bot123:SECRETSECRET/sendMessage")


def test_an_ordinary_line_passes_through_unchanged():
    line = "2026-09-12 00:00:00,703 INFO httpx — GET http://localhost:9010/tick/XAUUSD"
    assert scrub_log_secrets(line) == line


def test_an_asyncio_task_name_is_not_mistaken_for_a_secret():
    """174 lines of the live log read `name='Task-1004'`. A looser rule that
    went after anything token-shaped would redact the event-loop stall
    warnings, which are the evidence for bugs/030."""
    line = "WARNING asyncio — Executing <Task finished name='Task-1004' ...>"
    assert scrub_log_secrets(line) == line


def test_nothing_in_means_nothing_out():
    assert scrub_log_secrets("") == ""
    assert scrub_log_secrets(None) == ""
