"""Pytest plugin that pins the market clock, so the suite is reproducible.

Large parts of this suite depend on the wall-clock time of the machine running
it. `dpm_engine.detect_session()` reads `datetime.now(timezone.utc).hour`, and
`is_weekly_market_closed()` returns True all weekend. Anything behind the
session gate therefore passes Tuesday afternoon and fails Saturday night, with
no code change in between -- which makes "did my change break something?"
unanswerable on a Saturday.

This plugin pins both to a fixed, market-open instant so a run means the same
thing on any day. It patches the two clock readers rather than the global
`datetime`, because a blanket freeze would also stop timestamps, cache TTLs and
timeouts advancing, and several tests genuinely need those to move.

    python -m pytest tests/ -p tools.testing.fixed_clock

Default is Tuesday 2026-07-21 14:00 UTC -- inside the London/New York overlap,
so every session button is satisfied, and the same day the historical
"1624 passing" baseline was recorded. Override with:

    --market-clock=2026-07-21T09:00:00Z

This is a measurement instrument, not a fix. The underlying problem is that
production code reads the clock through a global rather than an injected
dependency; see docs/todo/refactor/phase-0-audit/QUESTIONS.md.
"""
from __future__ import annotations

from datetime import datetime, timezone

DEFAULT_CLOCK = "2026-07-21T14:00:00Z"


def pytest_addoption(parser):
    parser.addoption(
        "--market-clock", action="store", default=DEFAULT_CLOCK,
        help="UTC instant to pin the market clock to (ISO 8601, Z suffix). "
             "Default is a Tuesday inside the London/NY overlap.",
    )


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def pytest_configure(config):
    pinned = _parse(config.getoption("--market-clock"))
    from forex_trader.core import dpm_engine

    hour = pinned.hour
    london = 7 <= hour < 16
    ny = 12 <= hour < 21
    if london and ny:
        session = "overlap"
    elif london:
        session = "london"
    elif ny:
        session = "ny"
    else:
        session = "asian"

    # Recomputed from the pinned instant rather than hardcoded, so a custom
    # --market-clock stays self-consistent.
    dpm_engine.detect_session = lambda: session
    dpm_engine.is_weekly_market_closed = lambda now=None: _closed(now or pinned)

    config.addinivalue_line(
        "markers", "market_clock: suite ran against a pinned market clock")
    config._market_clock_summary = (
        f"market clock pinned to {pinned.isoformat()} "
        f"({pinned.strftime('%A')}, session={session})")


def _closed(now: datetime) -> bool:
    """Weekend rule mirrored from dpm_engine.is_weekly_market_closed."""
    wd = now.weekday()
    if wd == 5:
        return True
    if wd == 6:
        return now.hour < 22
    if wd == 4:
        return now.hour >= 21
    return False


def pytest_report_header(config):
    return getattr(config, "_market_clock_summary", "")
