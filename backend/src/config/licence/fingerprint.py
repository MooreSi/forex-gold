"""
Hardware fingerprint — produces the same registration_id as KeyGen/Registration/fingerprint.py.

Stable input (macOS):   platform_uuid | hw_uuid | serial | board_id
Stable input (Windows): machine_uuid | board_serial | cpu_processid

Output format: FOREX-{sha256[0:8]}-{sha256[8:16]}-{sha256[16:24]}-{sha256[24:32]}  (uppercase)

On macOS, MAC address and boot-volume UUID are excluded: both can change across
OS updates without any hardware change.  On Windows, hostname and NIC MAC are
excluded for the same reason; we use BIOS/UEFI UUIDs which survive OS reinstalls.

`TEST_WILDCARD` and `is_test_wildcard()` are defined below and **nothing in
this application consults either of them** (checked across the whole tree,
2026-08-31). The docstring here used to say the wildcard "bypasses all hardware
checks", which described a capability this codebase does not have and reads as
an invitation to add one. A licence bound to the wildcard would work on every
machine, so wiring it up is a licence bypass and needs the owner's sign-off.

They are kept rather than deleted because KeyGen may mirror the constant; the
question is recorded in docs/simon-handover/014. `tests/licence/
test_fingerprint.py::TestTheWildcardIsNotWired` fails if anything starts
consulting them.
"""
import hashlib
import platform
import re
import subprocess
import sys

TEST_WILDCARD = "TEST_WILDCARD_000000000000000000000000"


# ── macOS data collectors ─────────────────────────────────────────────────────

def _run(*args, timeout: int = 8) -> str:
    try:
        r = subprocess.run(list(args), capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def _extract(pattern: str, text: str, default: str = "") -> str:
    m = re.search(pattern, text, re.IGNORECASE)
    return m.group(1).strip() if m else default


def _macos_stable_input() -> str:
    """Build the stable_input string used for fingerprinting.
    Uses only manufacturing-time identifiers that survive macOS updates and disk reformats.
    Matches KeyGen/Registration/fingerprint.py _stable_input() v2.
    """
    iokit_raw = _run("ioreg", "-rd1", "-c", "IOPlatformExpertDevice")
    platform_uuid   = _extract(r'"IOPlatformUUID"\s*=\s*"([^"]+)"',          iokit_raw)
    platform_serial = _extract(r'"IOPlatformSerialNumber"\s*=\s*"([^"]+)"',  iokit_raw)
    board_id        = _extract(r'"board-id"\s*=\s*[<"]([^">]+)[">]',         iokit_raw)

    hw_raw  = _run("system_profiler", "SPHardwareDataType")
    hw_uuid = _extract(r"Hardware UUID:\s*([A-Z0-9-]+)",         hw_raw)
    serial  = _extract(r"Serial Number \(system\):\s*(\S+)",     hw_raw) or platform_serial

    parts = [platform_uuid, hw_uuid, serial, board_id]
    return "|".join(p.upper().strip() for p in parts)


# ── Windows stable input ──────────────────────────────────────────────────────

def _wmic_field(obj: str, field: str, timeout: int = 6) -> str:
    """Read a single WMI field via wmic.exe. Returns empty string on failure."""
    try:
        out = subprocess.run(
            ["wmic", obj, "get", field, "/value"],
            capture_output=True, text=True, timeout=timeout,
        ).stdout
        for line in out.splitlines():
            line = line.strip()
            if line.lower().startswith(field.lower() + "="):
                val = line.split("=", 1)[1].strip()
                if val and val.lower() not in ("", "none", "to be filled by o.e.m."):
                    return val
    except Exception:
        pass
    return ""


def _cim_field(classname: str, prop: str, timeout: int = 8) -> str:
    """Get a WMI field via PowerShell Get-CimInstance.

    Fallback for Windows 11 24H2+ where wmic.exe was removed.  Both wmic and
    Get-CimInstance query the same underlying WMI data, so the values returned
    are identical — the fingerprint is unchanged across the transition.
    """
    try:
        cmd = f"(Get-CimInstance -ClassName {classname}).{prop}"
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
            capture_output=True, text=True, timeout=timeout,
        ).stdout.strip()
        if out and out.lower() not in ("", "none", "to be filled by o.e.m."):
            return out
    except Exception:
        pass
    return ""


def _windows_stable_input() -> str:
    """
    Build a stable fingerprint string from BIOS/UEFI identifiers on Windows.

    Uses:
      - csproduct UUID  (system/motherboard UUID, set at manufacture)
      - baseboard SerialNumber  (board serial, set at manufacture)
      - cpu ProcessorId  (unique processor identifier)

    All three are stable across Windows reinstalls and updates.
    Tries wmic.exe first (Windows 10 / Windows 11 before 24H2); falls back to
    PowerShell Get-CimInstance for Windows 11 24H2+ where wmic was removed.
    Both interfaces query the same WMI store so values — and the fingerprint —
    are identical regardless of which path is taken.
    MAC address and hostname are excluded — both can change without hardware change.
    Must produce the same string as KeyGen/Registration/fingerprint.py _stable_input_windows().
    """
    machine_uuid = (_wmic_field("csproduct", "UUID")
                    or _cim_field("Win32_ComputerSystemProduct", "UUID"))
    board_serial  = (_wmic_field("baseboard", "SerialNumber")
                    or _cim_field("Win32_BaseBoard", "SerialNumber"))
    cpu_id        = (_wmic_field("cpu", "ProcessorId")
                    or _cim_field("Win32_Processor", "ProcessorId"))
    parts = [machine_uuid, board_serial, cpu_id]
    return "|".join(p.upper().strip() for p in parts)


# ── Linux / unknown fallback ──────────────────────────────────────────────────

def _fallback_stable_input() -> str:
    """Cross-platform fallback for Linux or environments where primary methods fail."""
    import uuid as _uuid
    raw = _uuid.getnode()
    mac = format(raw, "012x") if not (raw & (1 << 40)) else "000000000000"

    cpu = platform.processor() or "unknown_cpu"
    if not cpu and sys.platform.startswith("linux"):
        try:
            for line in open("/proc/cpuinfo"):
                if line.lower().startswith("model name"):
                    cpu = line.split(":", 1)[1].strip()
                    break
        except Exception:
            pass

    hostname = platform.node() or "unknown_host"

    board = ""
    if sys.platform.startswith("linux"):
        for path in ["/sys/class/dmi/id/board_serial", "/sys/class/dmi/id/product_serial"]:
            try:
                val = open(path).read().strip()
                if val and val not in ("None", "To be filled by O.E.M."):
                    board = val
                    break
            except Exception:
                pass

    return "|".join(p.upper().strip() for p in [mac, cpu, hostname, board])


# ── Public API ────────────────────────────────────────────────────────────────

def _compute_hash() -> str:
    if sys.platform == "darwin":
        stable = _macos_stable_input()
    elif sys.platform == "win32":
        stable = _windows_stable_input()
    else:
        stable = _fallback_stable_input()
    return hashlib.sha256(stable.encode()).hexdigest().upper()


def get_fingerprint() -> str:
    """
    Return the machine fingerprint in FOREX-XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX format.
    On macOS uses the same algorithm as KeyGen/Registration/fingerprint.py so the IDs match.
    """
    try:
        h = _compute_hash()
        return f"FOREX-{h[0:8]}-{h[8:16]}-{h[16:24]}-{h[24:32]}"
    except Exception:
        import uuid
        return f"FOREX-{hashlib.sha256(str(uuid.getnode()).encode()).hexdigest()[:32].upper()}"


def get_sha256() -> str:
    """Return the full 64-char uppercase SHA256 hash used to compute the fingerprint.
    Matches the 'sha256' field in regoutput.json from KeyGen/Registration/collect.command."""
    try:
        return _compute_hash()
    except Exception:
        import uuid
        return hashlib.sha256(str(uuid.getnode()).encode()).hexdigest().upper()


def is_test_wildcard(fingerprint: str) -> bool:
    return fingerprint == TEST_WILDCARD
