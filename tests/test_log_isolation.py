"""A test run must never write into the running app's log file.

WHY (2026-08-07)
----------------
run.py attached a rotating file handler to the ROOT logger at module scope,
aimed at the live forex_trader.log. tests/test_claim_port.py imports run, so
every pytest session quietly joined the running app's log.

The damage was not noise. One test run put five WARNINGs into the production
log reading "EA offline 601s with a healthy MT5 bridge -- restarting the
terminal (attempt 1/3)" and "terminal restart failed: wineserver would not
die". None of it happened: 601 was a fixture constant and the exception was
injected by a mock. That log is read precisely when something has gone wrong,
and it was describing an outage and a recovery attempt that never existed.

Two processes were also sharing one TimedRotatingFileHandler, so the app and
pytest would both attempt the midnight rename.

No MT5 order is ever placed, closed, or modified by any of this.
"""
import logging
import subprocess
import sys

from forex_trader.config import USER_DATA_DIR


def _app_dir_handlers() -> list[str]:
    """Every live handler writing anywhere under the app's data directory."""
    found = []
    for name in [None] + list(logging.root.manager.loggerDict):
        logger = logging.getLogger(name) if name else logging.getLogger()
        if not isinstance(logger, logging.Logger):
            continue
        for h in logger.handlers:
            path = getattr(h, "baseFilename", None)
            if path and str(USER_DATA_DIR) in str(path):
                found.append(str(path))
    return found


def test_no_handler_points_at_the_apps_data_directory():
    """The state every test runs under. If this fails, whatever ran before it
    has its log lines in the user's production log."""
    assert _app_dir_handlers() == []


def test_importing_run_does_not_hijack_the_root_logger():
    """The specific regression. `import run` must be inert -- logging is set up
    by main(), because importing a launcher is not consent to take over the
    root logger and start appending to a file another process is writing.

    Checked in a subprocess so this test cannot be satisfied merely by the
    conftest guard having already cleaned up after the import.
    """
    code = (
        "import logging, run;"
        "hs = [getattr(h, 'baseFilename', None) for h in logging.getLogger().handlers];"
        "print([h for h in hs if h])"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
    )

    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", (
        f"importing run attached file handler(s): {out.stdout.strip()}"
    )


def test_setup_logging_is_idempotent():
    """main() calls it, and a relaunch re-runs main() in the same interpreter
    under some restart paths. Attaching twice would write every line to the
    log twice, which reads exactly like a loop that is running twice."""
    import run

    before = len(logging.getLogger().handlers)
    try:
        run.setup_logging()
        after_first = len(logging.getLogger().handlers)
        run.setup_logging()
        run.setup_logging()
        assert len(logging.getLogger().handlers) == after_first
    finally:
        # Undo whatever the call attached: this test is the one place that
        # deliberately sets up real app logging, and it must not leak into the
        # rest of the session.
        for h in logging.getLogger().handlers[before:]:
            logging.getLogger().removeHandler(h)
            h.close()
        run._LOGGING_READY = False
