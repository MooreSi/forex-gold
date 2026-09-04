"""What the log says while a brand-new install is being activated.

A first install has no licence key — that is the normal, expected state, and
`enforce()` already says so at INFO ("No valid licence found — showing
activation screen"). `_show_error_and_exit` then logged a second line,
`ERROR ... Licence check failed:` with an empty reason, immediately after it.

That is not cosmetic. The activation flow is the one part of this app a user
watches in a terminal window, and when it does not finish they send that
window's contents in. A bare ERROR with nothing after the colon, on the
successful path, is the first thing they point at — it cost a real support
conversation on 2026-09-04. Only log an error when there is an error.
"""
from __future__ import annotations

import logging

import pytest

from backend.src.config.licence import guard


@pytest.fixture
def no_ui(monkeypatch):
    """The registration screen calls ui.run() and blocks; record instead."""
    seen: list = []
    monkeypatch.setattr(guard, "_show_registration_page",
                        lambda notice="": seen.append(notice))
    return seen


def _errors(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_first_install_logs_no_error(no_ui, caplog):
    caplog.set_level(logging.DEBUG, logger=guard.log.name)

    guard._show_error_and_exit("", allow_register=True)

    assert no_ui == [""], "the activation screen must still be shown"
    assert _errors(caplog) == []


def test_a_real_failure_still_logs_its_reason(no_ui, caplog):
    """The positive control: silencing the empty case must not silence the
    cases that are genuinely wrong."""
    caplog.set_level(logging.DEBUG, logger=guard.log.name)

    guard._show_error_and_exit(
        "Your licence expired on 2026-01-01.", allow_register=True
    )

    assert any("expired on 2026-01-01" in m for m in _errors(caplog)), _errors(caplog)
