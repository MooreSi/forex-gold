"""Restrict a secret file to its owner, on Windows as well as POSIX.

`os.chmod(path, 0o600)` protects nothing on Windows. It toggles a read-only
flag; the permission bits are ignored, and the file keeps whatever its parent
directory grants -- which on a normal install means every local account can
read it. Four places in this app wrote a secret and believed a chmod had
protected it:

    services/broker/credentials_repo.py   the MT5 login and PLAINTEXT password
    services/cluster/remote/ca.py         the private CA key
    config/secrets.py                     the key that decrypts the credentials
    config/licence/store.py               the licence

Windows clients are in scope (owner, 2026-09-02), so this is the one place that
knows how to do it per platform. Found 2026-09-02 from a Windows CI run
asserting `0o600` and getting `0o666`.

On Windows the tool is `icacls`, and BOTH halves matter:

    /inheritance:r   drop the ACEs inherited from the parent directory. This
                     is the half that actually removes "Users: read"; granting
                     the owner without it leaves everyone else's access intact.
    /grant:r USER:F  replace any existing grant for the owner with full access.

Never raises. Callers write a secret and then restrict it, and a failure here
must not turn one problem into a traceback on the trading path -- but it
returns False and logs loudly, because a caller told "protected" about a file
that is not is the failure this module exists to end.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["restrict_to_owner"]


def _is_windows() -> bool:
    """Indirection so tests can drive the Windows branch on a Mac.

    Patching `os.name` itself does not work: `pathlib` consults it when it
    builds a path, so a POSIX temp path turns into a backslash string and
    `exists()` starts returning False. The simulated Windows tests silently
    took the "file missing" branch instead of the one they meant to test.
    """
    return os.name == "nt"


def _current_user() -> str:
    """The account to grant. `USERNAME` is what icacls expects; getpass is the
    fallback for a service context where it is not set."""
    name = os.environ.get("USERNAME") or ""
    if not name:
        import getpass
        try:
            name = getpass.getuser()
        except Exception:
            name = ""
    domain = os.environ.get("USERDOMAIN") or ""
    return f"{domain}\\{name}" if domain and name else name


def _restrict_windows(path: Path) -> bool:
    user = _current_user()
    if not user:
        log.error("[perms] Cannot identify the current user — %s is left with "
                  "the permissions it inherited. Anyone with a local login on "
                  "this machine can read it.", path)
        return False
    try:
        proc = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{user}:F"],
            capture_output=True, text=True, timeout=20,
        )
    except Exception as exc:
        log.error("[perms] icacls could not be run (%s) — %s is left with the "
                  "permissions it inherited and may be readable by any local "
                  "account.", exc, path)
        return False
    if proc.returncode != 0:
        log.error("[perms] icacls failed on %s (rc=%s): %s — the file may be "
                  "readable by any local account.",
                  path, proc.returncode, (proc.stderr or "").strip()[:200])
        return False
    return True


def restrict_to_owner(path) -> bool:
    """Make `path` accessible to its owner alone.

    Returns True only if the restriction was actually applied, so a caller can
    tell the difference between "protected" and "we tried".
    """
    p = Path(path)
    if not p.exists():
        log.warning("[perms] %s does not exist — nothing to restrict", p)
        return False
    if _is_windows():
        return _restrict_windows(p)
    try:
        os.chmod(p, 0o600)
        return True
    except OSError as exc:
        log.error("[perms] chmod failed on %s: %s — the file may be readable "
                  "by other accounts.", p, exc)
        return False
