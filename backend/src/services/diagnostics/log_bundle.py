"""The support log bundle: the logs, minus the noise, with the context.

The NiceGUI app had an Export Logs button that read the rotated log files,
filtered them and emailed the result to a hardcoded address. The React port had
nothing, so the only way to get logs off a machine was to find them on disk.

**This builds the bundle; it does not send it anywhere.** A dashboard in a
browser can hand the operator the file, which needs no email provider
configured and puts nobody's address in the code. That difference from the
original is deliberate.

What makes a bundle useful is what it leaves out. A raw log is mostly
`HTTP Request: GET /tick/XAUUSD` at several lines a second and the errors are
somewhere inside it. Dropping the polling and the DEBUG lines is what turns
25 MB into something readable -- and the header states how much went, because
a quiet bundle otherwise reads as a quiet machine.
"""
from __future__ import annotations

import logging
import platform
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

__all__ = ["build", "DEFAULT_DAYS"]

DEFAULT_DAYS = 5
# 25 MB. The original's cap, chosen to stay under an email provider's limit;
# kept because a browser download of more than this is not something anybody
# reads either.
DEFAULT_MAX_BYTES = 25 * 1024 * 1024

# GET heartbeats to these paths fire continuously for as long as the app runs.
# Only GETs to these are dropped: a POST, or a failure, is exactly what
# somebody exporting logs is looking for.
#
# **Matched without the host or port.** The NiceGUI original spelled these
# `localhost:9000/positions`, and 9000 is the LIVE install's bridge port -- the
# demo bridge is on 9010, so on this machine none of the three ever matched and
# ~73,000 heartbeat lines went into every export. The port is configurable; it
# was never the right thing to match on.
_POLL_PATHS = (
    "/tick/XAUUSD", "/candles/XAUUSD", "/candles_symbol/",
    "/positions ", "/account ", "/health ",
    "/history?days=", "/history/position/", "getUpdates",
)

_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _line_ts(line: str) -> Optional[float]:
    m = _TS.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
    except ValueError:
        return None


def _keep(line: str) -> bool:
    if " DEBUG " in line:
        return False
    if "INFO httpx" in line and "HTTP Request: GET" in line:
        return not any(p in line for p in _POLL_PATHS)
    return True


def _log_files(directory: Path) -> list[Path]:
    """Rotated backups oldest first, then today's file.

    TimedRotatingFileHandler names backups `forex_trader.log.YYYY-MM-DD`, so an
    ascending sort is chronological. Read in the wrong order the bundle tells
    the story backwards.
    """
    base = directory / "forex_trader.log"
    rotated = sorted(directory.glob("forex_trader.log.2*"))
    return rotated + ([base] if base.exists() else [])


# A run of lines that differ only by their numbers has to be this long before
# the numbers are thrown away. Three trades opening in a row is a story worth
# reading in full; a hundred thousand skipped signals is a fact, and the ids
# are not what anybody reads.
_BURST_MIN = 20

_DIGITS = re.compile(r"\d+")


def _collapse(lines: list[str]) -> list[str]:
    """Collapse consecutive repeats, ignoring their timestamps.

    Two passes, because there are two kinds of flood:

    * **identical lines** -- `POST /modify` fires every five seconds for as
      long as a trade is open;
    * **lines differing only by an id** -- a real export carried 124,569 of
      "Skipping high-risk signal tg_id=<n>", 12% of the whole file, and the
      exact-match pass could not touch them.

    The second only applies above `_BURST_MIN` consecutive lines, so a short
    run of genuinely different events keeps every number it had.
    """
    exact: list[str] = []
    previous = None
    repeats = 0
    for line in lines:
        body = line[24:] if len(line) > 24 else line
        if body == previous:
            repeats += 1
            continue
        if repeats:
            exact.append(f"        ... repeated {repeats} more time(s)")
            repeats = 0
        exact.append(line)
        previous = body
    if repeats:
        exact.append(f"        ... repeated {repeats} more time(s)")

    out: list[str] = []
    run: list[str] = []
    run_shape = None

    def _flush() -> None:
        if not run:
            return
        if len(run) >= _BURST_MIN:
            out.append(run[0])
            out.append(f"        ... and {len(run) - 1} more similar lines "
                       f"(differing only by their numbers)")
        else:
            out.extend(run)
        run.clear()

    for line in exact:
        shape = _DIGITS.sub("N", line[24:] if len(line) > 24 else line)
        if shape == run_shape:
            run.append(line)
            continue
        _flush()
        run_shape = shape
        run.append(line)
    _flush()
    return out


def _app_context() -> dict:
    """Version and environment, for the header.

    Reads `utils.version_history` and `config` directly rather than through
    the controllers that also expose them: a service may not import a
    controller, and `utils`/`config` sit below everything.

    Separated so a failure to read any of it costs the header and not the
    logs -- which are the point.
    """
    import backend.src.config as cfg
    from backend.src.utils import version_history

    return {
        "version": version_history.__version__,
        "environment": str(cfg.get("account_env", "") or "demo").upper(),
    }


def _header(days: int, raw: int, kept: int, context: dict, when: float) -> str:
    return (
        "FOREX Trader — Filtered Log Export\n"
        + "=" * 80 + "\n"
        f"Generated  : {datetime.fromtimestamp(when):%Y-%m-%d %H:%M}\n"
        f"Period     : last {days} days\n"
        f"Raw lines  : {raw:,}  (scanned)\n"
        f"Kept lines : {kept:,}  (errors, warnings, app events)\n"
        f"Dropped    : {raw - kept:,}  (DEBUG + polling noise)\n"
        "\n-- System --\n"
        f"App version: {context.get('version', '—')}\n"
        f"Environment: {context.get('environment', '—')}\n"
        f"Python     : {sys.version.split()[0]}\n"
        f"OS         : {platform.system()} {platform.release()} ({platform.machine()})\n"
        + "=" * 80 + "\n\n"
    )


def build(directory=None, days: int = DEFAULT_DAYS, *,
          now: Optional[float] = None,
          max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """`{filename, text, raw_lines, kept_lines, truncated, note}`.

    `directory` defaults to the app's data directory, which is where the log
    handler writes. It is a parameter at all so a test can point it somewhere
    else -- and it defaults HERE rather than in the controller, because a
    controller that resolves a path is doing work instead of forwarding.

    Never raises on a missing or unreadable file: an export that fails because
    one rotated file is corrupt is worse than one that is missing a day.
    """
    when = time.time() if now is None else now
    if directory is None:
        from backend.src.config import DATA_DIR
        directory = DATA_DIR
    directory = Path(directory)
    cutoff = when - days * 86400

    raw_total = 0
    kept: list[str] = []
    # Whether the record currently being read is one to keep. Continuation
    # lines follow their header's decision. True to begin with, so a file that
    # starts mid-record does not silently lose its first lines.
    keeping = True
    for path in _log_files(directory):
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    stripped = line.rstrip()
                    if not stripped:
                        continue
                    raw_total += 1
                    ts = _line_ts(stripped)
                    if ts is None:
                        # A continuation line -- a stack trace, or the body of
                        # a multi-line report. It has no timestamp and no level
                        # of its own, so it belongs to whatever record last
                        # decided. Judging these individually kept the BODY of
                        # every dropped DEBUG record: the reconciliation pass
                        # logs its full report at DEBUG once the throttle has
                        # quietened it, and that alone put 13,918 lines into a
                        # real 5-day export, reading as 13,918 unresolved
                        # problems where there had been ten warnings.
                        #
                        # Before any header at all (a rotated file can begin
                        # mid-record) the line is kept: it is one line, and it
                        # might be the interesting one.
                        if keeping:
                            kept.append(stripped)
                        continue
                    if ts < cutoff:
                        keeping = False
                        continue
                    keeping = _keep(stripped)
                    if keeping:
                        kept.append(stripped)
        except OSError as exc:
            log.debug("[LogBundle] skipping %s: %s", path, exc)

    stamp = datetime.fromtimestamp(when).strftime("%Y%m%d_%H%M")
    filename = f"forex_trader_logs_{stamp}.txt"

    if not kept:
        note = ("No log file found — restart the app to begin writing one."
                if raw_total == 0 else
                f"No meaningful entries in the last {days} days "
                f"({raw_total:,} lines scanned — all routine polling).")
        return {"filename": filename, "text": note, "raw_lines": raw_total,
                "kept_lines": 0, "truncated": False, "note": note}

    collapsed = _collapse(kept)

    try:
        context = _app_context()
    except Exception as exc:
        log.debug("[LogBundle] context unavailable: %s", exc)
        context = {}

    header = _header(days, raw_total, len(collapsed), context, when)
    header_bytes = header.encode("utf-8")
    body_bytes = "\n".join(collapsed).encode("utf-8")
    truncated = False

    if len(header_bytes) + len(body_bytes) > max_bytes:
        # Keep the NEWEST entries: they are the ones that explain what just
        # went wrong. Realigned to a line boundary so the first kept line is
        # not half a line.
        marker = b"[OLDEST ENTRIES TRUNCATED - the file exceeded the size limit]\n\n"
        room = max_bytes - len(header_bytes) - len(marker)
        tail = body_bytes[-room:] if room > 0 else b""
        newline = tail.find(b"\n")
        if newline != -1:
            tail = tail[newline + 1:]
        body_bytes = marker + tail
        truncated = True

    return {
        "filename": filename,
        "text": (header_bytes + body_bytes).decode("utf-8", errors="replace"),
        "raw_lines": raw_total,
        "kept_lines": len(collapsed),
        "truncated": truncated,
        "note": "",
    }
