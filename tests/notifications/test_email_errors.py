"""What the operator is told when a mail send fails.

`535 5.7.139 Authentication unsuccessful` is Microsoft's way of saying "SMTP
AUTH is switched off on your Outlook account". Nobody guesses that, and the fix
is five clicks. A dashboard that prints the raw string has technically reported
the failure and practically not.

Two rules run through every test here:

  * **Specific before generic.** The Microsoft case is a 535 too, and the
    generic 535 advice sends the operator off to reset a password that was
    never the problem.
  * **The original is always attached.** A friendly message that HIDES the raw
    error makes an unrecognised failure unreportable — the operator cannot
    paste it anywhere, and neither can anyone helping them.
"""
from __future__ import annotations

import pytest

from backend.src.services.notifications.email_errors import friendly


class TestItSaysWhatToDo:
    def test_microsoft_basic_auth_gets_the_setting_that_is_off(self):
        out = friendly("535 5.7.139 Authentication unsuccessful")

        assert "Authenticated SMTP" in out
        assert "outlook.live.com" in out

    @pytest.mark.parametrize("raw", [
        "535 5.7.139 Authentication unsuccessful",
        "535 Basic authentication is disabled",
        "SMTPAuthenticationError: (535, b'5.7.139 ...')",
    ])
    def test_every_spelling_microsoft_uses_is_recognised(self, raw):
        assert "Authenticated SMTP" in friendly(raw)

    def test_gmail_is_told_to_use_an_app_password(self):
        out = friendly("535-5.7.8 Username and Password not accepted (gmail)")

        assert "App Password" in out
        assert "2-Step" in out

    def test_a_plain_535_says_it_is_the_password(self):
        out = friendly("535 authentication failed")

        assert "wrong username or password" in out
        assert "Authenticated SMTP" not in out, "that is the Microsoft advice"

    @pytest.mark.parametrize("raw", [
        "Connection refused", "Operation timed out", "Network is unreachable",
    ])
    def test_a_connection_failure_points_at_the_host_and_port(self, raw):
        assert "SMTP host and port" in friendly(raw)

    def test_a_tls_failure_points_at_starttls_and_the_port(self):
        out = friendly("SSL: CERTIFICATE_VERIFY_FAILED")

        assert "STARTTLS" in out
        assert "587" in out


class TestItNeverHidesTheOriginal:
    @pytest.mark.parametrize("raw", [
        "535 5.7.139 Authentication unsuccessful",
        "535 authentication failed",
        "Connection refused",
        "SSL: CERTIFICATE_VERIFY_FAILED",
    ])
    def test_the_raw_error_is_still_in_the_message(self, raw):
        assert raw[:40] in friendly(raw)

    def test_a_very_long_error_is_truncated_rather_than_dropped(self):
        out = friendly("535 " + "x" * 5000)

        assert "Original error" in out
        assert len(out) < 2000


class TestTheFallback:
    def test_an_error_it_does_not_recognise_is_passed_through(self):
        """Not swallowed and not guessed at. An unrecognised failure the
        operator can read is worth more than a confident wrong diagnosis."""
        out = friendly("550 Mailbox unavailable")

        assert "550 Mailbox unavailable" in out

    def test_a_send_that_failed_with_no_reason_still_says_something(self):
        """`send_email` returns `(False, "")` on some paths. "Failed: " with
        nothing after it reads as a broken dashboard."""
        assert friendly("") == "Failed: the mail server gave no reason."
