"""The EA version handshake.

The repo's mql5/ForexTraderBridge.mq5 and the terminal's compiled .ex5 are two
unlinked files, and nothing in MetaTrader reports that the build it is running
predates the source. This checks it from the EA's end on every connection, so
a stale build is a log line instead of a day spent on fixes that were never
loaded.

Mixed into EABridge -- see this package's __init__, which re-exports the
module-level names because the handshake tests read them off the package.
"""
from __future__ import annotations

from datetime import datetime
import logging
import re
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ── EA version handshake (2026-08-05) ────────────────────────────────────────
# The repo's mql5/ForexTraderBridge.mq5 and the terminal's compiled .ex5 are
# two unlinked files; nothing in MetaTrader reports that the build it is
# running predates the source. tools/deploy_ea.sh catches that on disk, but
# only for the terminals on the machine you happen to run it on, and only if
# you remember to run it. This catches it from the other end: the EA states
# its own version on every connection and we check it against the source we
# were shipped with, so a stale build is a log line instead of a day of
# fixes that were never loaded.
def _repo_root() -> Path:
    """The checkout root (the directory holding run.py).

    Walks up for the marker rather than counting parents: upstream counted two
    from forex_trader/core/, but this module now sits at
    backend/src/services/broker/, so the fixed index resolved to backend/src
    and the EA source lookup pointed at a file that does not exist -- the
    version handshake then reported every EA as stale. Found by
    tests/core/test_ea_bridge_version_handshake.py in the 2026-08-25 merge."""
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "run.py").exists():
            return candidate
    return here.parents[2]


_EA_SOURCE = _repo_root() / "mql5" / "ForexTraderBridge.mq5"
_EA_VERSION_RE = re.compile(r'^\s*#define\s+EA_VERSION\s+"([^"]+)"', re.M)
# MetaEditor stamps __DATETIME__ in local time, and this compares it to a
# local mtime -- sound only because the EA and this process always share a
# machine (see module docstring).
_EA_COMPILED_FMT = "%Y.%m.%d %H:%M:%S"
# The .ex5 is written when you press F7, the .mq5 when you save. Saving a
# file a second or two after the compile that read it is normal and means
# nothing; treat only a clear gap as evidence of an uncompiled edit.
_EA_COMPILE_SLACK_S = 120.0


def _expected_ea_version() -> Optional[str]:
    """EA_VERSION as declared by the repo copy of the EA source, or None if
    that source isn't present -- a packaged/frozen install ships the .ex5
    without the .mq5, and has nothing to compare against. Deliberately
    uncached: the source changes under a long-running dev process far more
    often than an EA reconnects, and reading ~100KB once per connection
    costs nothing.
    """
    try:
        m = _EA_VERSION_RE.search(_EA_SOURCE.read_text(errors="replace"))
    except OSError:
        return None
    return m.group(1) if m else None


class VersionMixin:
    """EABridge's version-handshake method. Not instantiated on its own."""

    def _check_ea_version(self, msg: dict) -> None:
        """Compare the connecting EA's self-reported build against the EA
        source this app was shipped with, and say so loudly when they differ.

        ea_version_ok tracks the version comparison alone: True/False when
        there is a source version to compare, None when there isn't. The
        source-newer-than-binary check below is advisory and deliberately
        does NOT flip it false -- it fires on any unsaved-then-saved edit,
        including ones that never reach a terminal, so it is worth a warning
        but not worth anything downstream branching on.

        Only ever logs. A stale EA is still a working EA -- it manages trades
        with whatever rules it was compiled with -- so refusing to talk to it
        would turn "some fixes aren't live" into "nothing is managed", which
        is strictly worse. The point is that the mismatch stops being silent.
        """
        self.ea_version = msg.get("ea_version") or None
        self.ea_compiled = msg.get("compiled") or None
        expected = _expected_ea_version()

        if self.ea_version is None:
            self.ea_version_ok = False
            log.warning(
                "[EABridge] EA sent no version in hello -- it predates the "
                "version handshake (expected v%s). Deploy and recompile: "
                "tools/deploy_ea.sh", expected or "?")
            return

        if expected is None:
            # Packaged install with no .mq5 alongside. Record what connected
            # so it still shows up in logs, but there's nothing to check.
            self.ea_version_ok = None
            log.info("[EABridge] EA v%s (compiled %s); no EA source present "
                     "to check it against", self.ea_version, self.ea_compiled)
            return

        self.ea_version_ok = (self.ea_version == expected)
        if not self.ea_version_ok:
            log.warning(
                "[EABridge] EA VERSION MISMATCH: terminal is running v%s "
                "(compiled %s) but this app ships EA source v%s. The .ex5 is "
                "stale -- run tools/deploy_ea.sh, then compile (F7).",
                self.ea_version, self.ea_compiled, expected)
            return

        # Versions agree, so check the weaker signal too: an edit made after
        # the last compile that didn't move EA_VERSION is invisible to the
        # comparison above, but does show up as source newer than binary.
        try:
            compiled_at = datetime.strptime(self.ea_compiled or "", _EA_COMPILED_FMT)
            src_mtime = datetime.fromtimestamp(_EA_SOURCE.stat().st_mtime)
        except (ValueError, OSError):
            compiled_at = src_mtime = None
        if compiled_at is not None and src_mtime is not None:
            drift = (src_mtime - compiled_at).total_seconds()
            if drift > _EA_COMPILE_SLACK_S:
                log.warning(
                    "[EABridge] EA v%s matches, but the source was modified "
                    "%.0f min after this build was compiled (%s) -- there are "
                    "edits the running EA does not have. Recompile (F7).",
                    self.ea_version, drift / 60.0, self.ea_compiled)
                return

        log.info("[EABridge] EA v%s (compiled %s, MQL build %s, terminal "
                 "build %s)", self.ea_version, self.ea_compiled,
                 msg.get("mql_build"), msg.get("terminal_build"))
