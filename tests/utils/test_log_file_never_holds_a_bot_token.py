"""The bot token is scrubbed before a log line is written, not after.

httpx logs every request URL at INFO, and a Telegram bot URL carries the token
in its path. `scrub_log_secrets` (2026-09-12) cleans it from the diagnostics
upload, but the local log file itself still held it in full -- found again on
2026-09-24 in the live forex_trader.log, on every getUpdates poll. That file is
what "Export Logs" emails and what anyone asked to look at the logs is sent.

A filter on the app's own handlers now rewrites each record before any
handler writes it. The bot id stays (support needs to know which bot); the
secret after the colon does not.
"""
from __future__ import annotations

import io
import logging

import run

URL = ("GET https://api.telegram.org/bot1234567890:AAH_fakeTokenForTestsOnly_x0x0x0x0x0x"
       "/getUpdates?offset=1&timeout=10")


def _logger_with(handler) -> logging.Logger:
    lg = logging.getLogger("test.scrub." + str(id(handler)))
    lg.handlers[:] = [handler]
    lg.propagate = False
    lg.setLevel(logging.INFO)
    return lg


def _stream_handler():
    buf = io.StringIO()
    h = logging.StreamHandler(buf)
    h.setFormatter(logging.Formatter("%(message)s"))
    return buf, h


def test_the_secret_never_reaches_the_handler_output():
    buf, h = _stream_handler()
    run._attach_secret_scrubber(_logger_with(h))

    logging.getLogger("test.scrub." + str(id(h))).info('HTTP Request: %s "%s"', URL, "HTTP/1.1 200 OK")

    out = buf.getvalue()
    assert "AAH_fakeTokenForTestsOnly_x0x0x0x0x0x" not in out
    assert "bot1234567890:***" in out
    assert "getUpdates" in out and "200 OK" in out


def test_an_ordinary_line_is_left_exactly_as_it_was():
    buf, h = _stream_handler()
    lg = _logger_with(h)
    run._attach_secret_scrubber(lg)

    lg.info("[RE-ML] retrained — n=%d backend=%s", 5640, "lgb")

    assert buf.getvalue() == "[RE-ML] retrained — n=5640 backend=lgb\n"


def test_attaching_twice_does_not_stack_filters():
    buf, h = _stream_handler()
    lg = _logger_with(h)
    run._attach_secret_scrubber(lg)
    run._attach_secret_scrubber(lg)

    assert len(h.filters) == 1


def test_setup_logging_attaches_it():
    import inspect
    assert "_attach_secret_scrubber(" in inspect.getsource(run.setup_logging)
