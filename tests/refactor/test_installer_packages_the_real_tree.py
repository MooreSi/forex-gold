"""The Windows installer ships the tree that actually exists.

installer/FOREX_Trader_Setup.iss still packaged `..\\forex_trader\\*` and
took its icons from `..\\forex_trader\\ui\\static\\` — a directory deleted
when the repo was restructured. Inno Setup fails at COMPILE time on a
[Files] entry that matches nothing, so the installer had been unbuildable
since the restructure and nothing said so: no Python imports it, no test
touched it, and the last built .exe still sits in the repo root looking
like evidence it works.

This test reads the .iss and asserts every source path resolves. It is
deliberately a path check rather than a build: Inno Setup is Windows-only
and cannot run here, but a missing path is the failure mode that actually
bit, and it is checkable anywhere.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ISS = REPO / "installer" / "FOREX_Trader_Setup.iss"


def _iss_text() -> str:
    return ISS.read_text(encoding="utf-8", errors="replace")


def _source_paths() -> list[str]:
    """Every `Source: "..."` value in the [Files] section."""
    return re.findall(r'Source:\s*"([^"]+)"', _iss_text())


def _icon_paths() -> list[str]:
    """Icon paths, which live outside [Files] and are just as breakable."""
    text = _iss_text()
    found = re.findall(r'IconFilename:\s*"([^"]+)"', text)
    found += re.findall(r'SetupIconFile\s*=\s*(\S+)', text)
    found += re.findall(r'UninstallDisplayIcon\s*=\s*(\S+)', text)
    return found


def _resolve(raw: str) -> Path | None:
    """Map an Inno path to a repo path, or None if it is not repo-relative.

    `{app}\\x` is an install-time location, so only its tail is checkable;
    `..\\x` is relative to installer/.
    """
    cleaned = raw.strip().strip('"')
    cleaned = cleaned.replace("{app}\\", "").replace("{#SourceDir}\\", "")
    if cleaned.startswith("{"):
        return None                      # a pure Inno constant, nothing to check
    cleaned = cleaned.replace("\\", "/")
    # Inno resolves relative sources against the script's own directory
    # (SourceDir defaults to it), so both `../x` and a bare `x` are
    # installer/-relative. A path that only exists once installed --
    # `{app}\...` stripped above -- is checked against the repo root
    # instead, since that is where it was packaged from.
    local = (ISS.parent / cleaned).resolve()
    if local.exists() or cleaned.startswith("../"):
        return local
    return (REPO / cleaned).resolve()


def _exists(path: Path) -> bool:
    """A trailing `*` means "this directory's contents"."""
    if path.name == "*":
        return path.parent.is_dir()
    return path.exists()


def test_the_installer_script_is_present():
    assert ISS.is_file(), "the installer script itself is missing"


@pytest.mark.parametrize("raw", _source_paths())
def test_every_packaged_source_path_exists(raw):
    resolved = _resolve(raw)
    if resolved is None:
        pytest.skip(f"{raw} is an Inno constant")
    assert _exists(resolved), (
        f"installer packages {raw!r}, which resolves to {resolved} and does "
        f"not exist. Inno Setup fails at compile time on this."
    )


@pytest.mark.parametrize("raw", _icon_paths())
def test_every_icon_path_exists(raw):
    resolved = _resolve(raw)
    if resolved is None:
        pytest.skip(f"{raw} is an Inno constant")
    assert _exists(resolved), f"installer references missing icon {raw!r} ({resolved})"


def test_the_installer_ships_the_current_top_level_packages():
    """The restructure's whole point: backend/ and frontend/ are the app."""
    sources = " ".join(_source_paths()).lower()
    for required in ("backend", "frontend", "run.py"):
        assert required.lower() in sources, (
            f"installer does not ship {required} -- the app will not run"
        )


def test_the_installer_no_longer_references_the_deleted_tree():
    """Paths only. A comment explaining that forex_trader/ was replaced is
    history worth keeping -- it is a path pointing at it that breaks the
    build."""
    code = "\n".join(
        line for line in _iss_text().splitlines()
        if not line.lstrip().startswith(";")
    )
    assert "forex_trader" not in code, (
        "forex_trader/ was deleted in the restructure; every path referencing "
        "it in the installer is broken"
    )


def test_the_installer_ships_version_and_changelog():
    """update_panel reads both at runtime; shipping without them makes the
    in-app updater report nothing."""
    sources = " ".join(_source_paths()).upper()
    assert "VERSION" in sources
    assert "CHANGELOG.MD" in sources
