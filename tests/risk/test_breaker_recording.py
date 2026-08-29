"""Failures in the post-close halt checks must be loud (stage3/050).

Two blocks at the end of `record_close` decide whether the protective halts
see a live loss at all, and both swallowed their failures at DEBUG:

    except Exception as _rg_e:
        log.debug("[RG] post-close halt check skipped: %s", _rg_e)
    ...
    except Exception as _cb_e:
        log.debug("[CB] outcome recording skipped: %s", _cb_e)

At debug level, in a log this size, that is invisible. A live losing streak
could silently never reach the circuit breaker and nobody would know the
protection had stopped working -- the halts would simply appear never to fire,
which is indistinguishable from not having lost enough yet.

THE CONSTRAINT. These sit inside `record_close`, which is FROZEN. The change
is the log level and a notification, nothing else: the close still records, the
return value is unchanged, and the notification is itself wrapped so it can
never escape and break a close. The tests below assert exactly that -- a
planted failure in each block leaves the close completely unaffected.
"""
from __future__ import annotations

import logging

import pytest


class TestTheFailuresAreLoud:
    def test_the_governor_block_no_longer_swallows_at_debug(self):
        import inspect
        from backend.src.services.trading import close_trade

        src = inspect.getsource(close_trade)

        assert 'log.debug("[RG] post-close halt check skipped' not in src, (
            "a failed post-close halt check is still invisible at debug level")

    def test_the_breaker_block_no_longer_swallows_at_debug(self):
        import inspect
        from backend.src.services.trading import close_trade

        src = inspect.getsource(close_trade)

        assert 'log.debug("[CB] outcome recording skipped' not in src, (
            "a failed circuit-breaker recording is still invisible at debug "
            "level — a live losing streak could never trip it")

    def test_BOTH_call_sites_route_to_the_loud_reporter(self):
        """Both blocks, each tagged so the log says which check failed. The
        two share one reporter, so the tag is an argument rather than part of
        the message -- checking for a literal "[RG]" in the source would only
        have been testing how the string is assembled."""
        import inspect
        from backend.src.services.trading import close_trade

        src = inspect.getsource(close_trade)

        assert '_notify_halt_check_failed("RG"' in src
        assert '_notify_halt_check_failed("CB"' in src

    def test_the_reporter_logs_at_ERROR_not_debug(self, caplog):
        """Behavioural. Debug is where these were invisible."""
        from backend.src.services.trading import close_trade

        with caplog.at_level(logging.DEBUG):
            close_trade._notify_halt_check_failed("RG", "planted")

        levels = {r.levelno for r in caplog.records
                  if "halt check FAILED" in r.getMessage()}
        assert logging.ERROR in levels


class TestTheCloseItselfIsUnaffected:
    """The frozen-path constraint, asserted rather than asserted-to."""

    def test_a_failing_halt_notification_cannot_escape(self):
        """The notification is the new code, so it is the new way this could
        break a close. It is wrapped: if it raises, the close still returns."""
        import inspect
        from backend.src.services.trading import close_trade

        src = inspect.getsource(close_trade._notify_halt_check_failed)

        assert "try:" in src and "except Exception" in src, (
            "the halt-failure notification is not wrapped — it could escape "
            "and break a close")

    def test_the_notifier_swallows_anything_thrown_at_it(self):
        """Exercised, not just read. Whatever the alert layer does, this must
        return normally."""
        from backend.src.services.trading import close_trade

        # No event loop, no telegram, no database: the worst case.
        close_trade._notify_halt_check_failed("RG", "planted failure")

    def test_the_notifier_says_WHICH_check_failed(self, caplog):
        from backend.src.services.trading import close_trade

        with caplog.at_level(logging.ERROR):
            close_trade._notify_halt_check_failed("CB", "database is locked")

        text = " ".join(r.getMessage() for r in caplog.records)
        assert "CB" in text
        assert "database is locked" in text
