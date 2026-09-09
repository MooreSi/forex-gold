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


def template_blocked_by_stale_build(ea, strategy: str):
    """Reason a TEMPLATE must not open on this EA build, or None.

    bugs/033, on the owner's instruction. An EA Template IS an EA-native
    management definition -- `is_strategy_portable` returns True for every
    template precisely because there is no Python-managed equivalent to fall
    back to -- so the whole management of a template trade is whatever build
    sits on the chart. Opening one against a stale `.ex5` runs it under logic
    this app has already replaced.

    Templates only. The EA-portable strategies have a Python fallback, and
    failing them here would silently reroute rather than refuse, hiding the
    problem instead of surfacing it.

    Unknown is not stale (`ea_version_ok` is None with no source to compare
    against), no EA at all is `open_trade`'s own "requires a connected, healthy
    EA" rather than a second error for one cause, and a bridge that throws does
    not block -- this sits on the order path.
    """
    try:
        from backend.src.services.broker.ea_templates import is_template_override
        if ea is None or not is_template_override(strategy):
            return None
        if getattr(ea, "ea_version_ok", None) is not False:
            return None
        running = getattr(ea, "ea_version", None) or "unknown"
        expected = _expected_ea_version() or "unknown"
        return (
            f"EA Template refused: the chart is running EA v{running} but this "
            f"app ships v{expected}. A template is managed entirely by the EA, "
            f"so a stale build would run it under replaced logic. "
            f"Fix: run tools/deploy_ea.sh, compile (F7) and re-attach the EA."
        )
    except Exception:
        return None


def ea_build_status() -> tuple[bool, str]:
    """Is the terminal running a stale .ex5? Returns (stale, detail).

    The handshake in `_version.py` has always logged this, and on 2026-09-09
    that was not enough: the owner recompiled, re-attached, saw the old
    behaviour, and the explanation sat in a WARNING nobody was reading. This
    exposes the same state for the top-bar EA badge, because a green badge on a
    stale build is the screen contradicting the log.

    `ea_version_ok` is None when there is no EA source to compare against (a
    packaged install). Unknown is NOT stale -- crying wolf there would train
    the badge to be ignored, which is how the log warning stopped working.

    Never raises: this runs on the header's refresh tick.
    """
    try:
        from backend.src.services.broker import ea_bridge
        bridge = ea_bridge.get_instance()
        if bridge is None or getattr(bridge, "ea_version_ok", None) is not False:
            return False, ""
        running = getattr(bridge, "ea_version", None) or "unknown"
        expected = _expected_ea_version() or "unknown"
        return True, (
            f"The EA on the chart is v{running}, but this app ships v{expected}. "
            f"The compiled .ex5 is stale, so EA fixes are NOT running. "
            f"Fix: run tools/deploy_ea.sh, then compile in MetaEditor (F7) and "
            f"re-attach the EA."
        )
    except Exception:
        return False, ""


def ea_badge_state(ea_ok: bool, stale: bool, scope: str, stale_detail: str) -> tuple:
    """(colour, text, tooltip) for the top-bar EA badge.

    A pure function so the DECISION can be tested rather than the presence of a
    colour string in the render: a mutation making the amber branch unreachable
    left "orange" in the source and passed a grep-based test.

    Stale outranks connected, and is amber rather than green or red: the EA IS
    connected and managing trades, but it is not the build this app ships, so
    every EA fix since is absent. A disconnected EA is not running any build, so
    "not connected" outranks both.
    """
    if not ea_ok:
        return "red", "EA", (
            f"EA not connected on {scope} — trades still work, falling back to "
            "Python-managed instead of native on-tick management")
    if stale:
        return "orange", "EA STALE BUILD", stale_detail
    return "green", "EA", (
        f"EA connected on {scope} — trades can be managed natively in MT5")
