"""Getting the repo's EA onto this machine's MetaTrader terminals, with
nobody in front of them.

The repo copy of `mql5/ForexTraderBridge.mq5` and a terminal's compiled `.ex5`
are two unlinked files. Nothing in MetaTrader reports that the build it is
running predates the source, which is how a day of correct EA fixes was spent
against a build from three weeks earlier (see the header of
`tools/deploy_ea.sh`, the hand-run macOS-only ancestor of this module).

`services/positions/core_app_update.apply_update` already pulls the whole repo
onto every remote machine, so the EA SOURCE arrives there on its own. What did
not happen automatically is the three steps after that:

  copy      HERE. Every MQL5/Experts folder on this machine, Windows roaming
            profiles and macOS/CrossOver bottles alike, verified
            byte-identical after writing.
  compile   WINDOWS ONLY (`compile_ea`). metaeditor64.exe /compile works
            headlessly there. It does NOT work under CrossOver: it exits 0,
            writes no log and rebuilds nothing, so this refuses on macOS
            rather than reporting a success that did not happen.
  attach    NOT POSSIBLE from here. Attaching an EA to a chart needs a chart
            template at terminal start; there is no runtime API for it. What
            matters in practice is an ALREADY attached EA picking up a new
            .ex5, which the terminal does itself.

**The portable answer is to ship a compiled `.ex5` in the repo** next to the
`.mq5`. Then a remote machine needs no MetaEditor at all: the pull brings the
binary, this drops it into Experts, and the running EA reloads. Compile once,
here, in the same change that bumps EA_VERSION -- see the broker domain README.

Deliberately different from `tools/deploy_ea.sh` in one respect: the script
only writes to folders that ALREADY hold a copy of the EA, so a hand-run can
never scatter the file into an unrelated terminal. That rule would leave a
freshly installed remote machine -- the exact case this exists for -- with
nothing. Every discovered Experts folder is a target here instead, which is
safe because the file is inert until someone attaches it to a chart.
"""
from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

EA_NAME = "ForexTraderBridge"

# Where MetaTrader keeps a terminal's MQL5 tree, relative to $HOME. Globs, not
# fixed paths: the terminal id is a hash of the install and there is typically
# more than one per machine (a live one, a demo one, and which is in use
# changes). The CrossOver shapes carry the Windows layout inside a bottle's
# fake C: drive, and the bottle and user names are both free text.
_HOME_GLOBS = (
    # Windows, and the same layout inside any Wine/CrossOver prefix.
    "AppData/Roaming/MetaQuotes/Terminal/*/MQL5/Experts",
    "Library/Application Support/CrossOver/Bottles/*/drive_c/users/*/"
    "AppData/Roaming/MetaQuotes/Terminal/*/MQL5/Experts",
    # MetaQuotes' own macOS wrapper, which uses its own bundled prefix.
    "Library/Application Support/MetaTrader 5/Bottles/*/drive_c/users/*/"
    "AppData/Roaming/MetaQuotes/Terminal/*/MQL5/Experts",
    "Library/Application Support/MetaQuotes/Terminal/*/MQL5/Experts",
)


def experts_dirs(home: Optional[Path] = None) -> list[Path]:
    """Every MQL5/Experts directory on this machine.

    Read-only, and creates nothing: a deploy that has to make an Experts
    folder has found a terminal that does not exist.
    """
    root = Path(home) if home is not None else Path.home()
    found: set[Path] = set()
    for pattern in _HOME_GLOBS:
        try:
            for p in root.glob(pattern):
                if p.is_dir():
                    found.add(p)
        except OSError as e:      # an unreadable bottle must not stop the rest
            log.debug("[EADeploy] glob %s failed: %s", pattern, e)
    return sorted(found)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _copy_verified(src: Path, dst: Path) -> str:
    """Copy src over dst, keeping what was there and proving what landed.

    Returns "current" (nothing to do), "deployed", or raises. The verify is
    not ceremony: a half-written EA that still compiles is worse than one that
    fails to, and shutil's return code does not prove the bytes.
    """
    src_sum = _sha256(src)
    if dst.exists() and _sha256(dst) == src_sum:
        return "current"
    if dst.exists():
        # Not under version control -- this backup is its only trace.
        backup = dst.with_name(f"{dst.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(dst, backup)
    shutil.copy(src, dst)
    if _sha256(dst) != src_sum:
        raise OSError(f"copy verify failed: {dst} does not match {src}")
    return "deployed"


def _needs_compile(mq5: Path, ex5: Path) -> bool:
    """An .ex5 missing, or older than the .mq5 beside it, is a build nobody
    compiled -- the condition that hid the original problem."""
    if not ex5.exists():
        return True
    if not mq5.exists():
        return False
    return ex5.stat().st_mtime < mq5.stat().st_mtime


def deploy(repo_root: Optional[Path] = None, home: Optional[Path] = None) -> dict:
    """Push the repo's EA into every terminal on this machine.

    Returns a report: {targets, deployed, already_current, needs_compile,
    errors}. Never raises -- see deploy_after_update for why.

    A missing repo `.mq5` is not an error (a packaged install ships the
    binary without the source) and neither is a missing `.ex5`. Both are
    simply not copied: overwriting a terminal's working file with nothing, or
    deleting its locally compiled binary, would take the bridge down on every
    remote machine at once.
    """
    root = Path(repo_root) if repo_root is not None else _repo_root()
    src_mq5 = root / "mql5" / f"{EA_NAME}.mq5"
    src_ex5 = root / "mql5" / f"{EA_NAME}.ex5"
    targets = experts_dirs(home=home)

    report = {"targets": [str(t) for t in targets], "deployed": 0,
              "already_current": 0, "needs_compile": 0, "errors": []}

    for target in targets:
        dst_mq5 = target / f"{EA_NAME}.mq5"
        dst_ex5 = target / f"{EA_NAME}.ex5"
        try:
            if src_mq5.exists():
                outcome = _copy_verified(src_mq5, dst_mq5)
                report["deployed" if outcome == "deployed" else "already_current"] += 1
            if src_ex5.exists():
                _copy_verified(src_ex5, dst_ex5)
            if _needs_compile(dst_mq5, dst_ex5):
                report["needs_compile"] += 1
        except OSError as e:
            # One unwritable terminal must not cost the others theirs.
            log.warning("[EADeploy] %s: %s", target, e)
            report["errors"].append(f"{target}: {e}")
    return report


def _repo_root() -> Path:
    from backend.src.utils.os_utils import repo_root
    return Path(repo_root())


def _metaeditor_path() -> Optional[Path]:
    """MetaEditor's executable, or None. Windows only -- see compile_ea."""
    for candidate in (
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "MetaTrader 5" / "metaeditor64.exe",
        Path(r"C:\Program Files\MetaTrader 5\metaeditor64.exe"),
    ):
        if candidate.exists():
            return candidate
    found = shutil.which("metaeditor64.exe")
    return Path(found) if found else None


def compile_ea(experts_dir: Path, platform: Optional[str] = None) -> dict:
    """Compile the EA in `experts_dir`. Returns {"ok": bool, "detail": str}.

    Windows only, and it says so rather than trying elsewhere. MetaEditor's
    /compile does not work headlessly under CrossOver: it exits 0, writes no
    log, and rebuilds nothing -- an "ok" from it would be exactly the silent
    staleness this whole module exists to end. On macOS the answer is a
    pre-compiled .ex5 shipped in the repo (see the module docstring), not a
    compile here.
    """
    plat = platform if platform is not None else sys.platform
    if plat != "win32":
        return {"ok": False, "detail": (
            "compiling is Windows-only: MetaEditor's /compile does nothing "
            "under CrossOver/Wine (exits 0, writes no log, rebuilds nothing). "
            "Ship a pre-compiled .ex5 in mql5/ instead.")}

    editor = _metaeditor_path()
    if editor is None:
        return {"ok": False, "detail": "metaeditor64.exe not found on this machine"}

    src = Path(experts_dir) / f"{EA_NAME}.mq5"
    log_file = Path(experts_dir) / f"{EA_NAME}.compile.log"
    try:
        proc = subprocess.run(
            [str(editor), f"/compile:{src}", f"/log:{log_file}"],
            capture_output=True, text=True, timeout=180, check=False,
        )
    except Exception as e:
        return {"ok": False, "detail": f"metaeditor failed to run: {e}"}

    # MetaEditor's exit code is not evidence -- it is 0 on a build it never
    # performed. The .ex5 landing NEWER than the .mq5 is.
    ex5 = Path(experts_dir) / f"{EA_NAME}.ex5"
    if _needs_compile(src, ex5):
        return {"ok": False, "detail": (
            f"metaeditor exited {proc.returncode} but {ex5.name} is still "
            f"missing or older than the source -- nothing was rebuilt")}
    return {"ok": True, "detail": f"compiled {ex5.name}"}


def deploy_after_update(repo_root: Optional[Path] = None,
                        home: Optional[Path] = None) -> dict:
    """The hook `apply_update` calls once the pull has landed.

    Swallows everything. The app update has already succeeded by the time this
    runs, and a failed EA copy must not turn a good update into a failed one --
    the EA keeps running whatever it was compiled with, which is the state the
    machine was already in.
    """
    try:
        report = deploy(repo_root=repo_root, home=home)
    except Exception as e:
        log.warning("[EADeploy] deploy failed after update: %s", e)
        return {"ok": False, "error": str(e)}
    if report["errors"]:
        log.warning("[EADeploy] deployed with errors: %s", report["errors"])
    log.info("[EADeploy] %d terminal(s): %d updated, %d already current, "
             "%d awaiting a compile",
             len(report["targets"]), report["deployed"],
             report["already_current"], report["needs_compile"])
    return {"ok": True, "report": report}


def reload_decision(*, ea_version_ok: Optional[bool], slots_in_use: int,
                    can_reload: bool, already_tried: bool) -> dict:
    """Should the terminal be restarted so MT5 loads a newly deployed EA?

    Returns {"reload": bool, "reason": str}. Pure -- the caller owns the
    restart, the alert and the state.

    Restarting is the ONLY way to load a new EA from outside: attaching an
    expert to a chart has no runtime API, and MT5 restores its charts (and the
    expert on them) when it comes back. core_ea_link_watchdog already pulls
    that lever for a dead EA.

    This fires on a different fact, and the difference is the whole reason for
    the guards. There the EA is silent and a restart costs nothing that is not
    already lost. Here it is alive, healthy, and managing trades -- just the
    wrong build -- and a restart blinds management for the ~2 minutes MT5
    takes to cold-start, log in and reload. Cheap when nothing is at risk,
    expensive when something is.

      ea_version_ok   False means the running EA is not the repo's. None means
                      there was nothing to compare against (a packaged install
                      ships the .ex5 without the .mq5) -- unknown is not
                      evidence, and never triggers a restart.
      slots_in_use    signal_state_repo.count_trade_slots_used: open
                      positions, orders resting at the broker, opens in
                      flight. Anything above zero defers -- a newer EA is
                      worth two minutes of nothing only when there is nothing
                      to manage.
      can_reload      only the macOS/Wine bridge tears the terminal down.
                      Windows/native restarts mt5_bridge.py alone, the
                      terminal keeps running and the expert is never reloaded,
                      so a restart would drop the bridge for no gain.
      already_tried   once per process. On macOS nothing can compile, so a new
                      .mq5 with no .ex5 beside it reloads the SAME build and
                      reports the same stale version -- without this cap that
                      is a terminal restart every cycle, forever.
    """
    if ea_version_ok is not False:
        return {"reload": False, "reason": "the running EA is not known to be stale"}
    if not can_reload:
        return {"reload": False, "reason": (
            "restarting this bridge would not reload the expert (Windows/native "
            "keeps the terminal running) — load it by hand")}
    if already_tried:
        return {"reload": False, "reason": (
            "already restarted once for this stale build — it needs a look "
            "(compiled? on the chart? AutoTrading on?)")}
    if slots_in_use:
        return {"reload": False, "reason": (
            f"a newer EA is deployed but {slots_in_use} trade slot(s) are in use — "
            f"holding the restart until the book is empty")}
    return {"reload": True, "reason": "a newer EA is deployed and nothing is open"}
