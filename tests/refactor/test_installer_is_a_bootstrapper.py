"""The Windows installer carries no app: it fetches it from GitHub.

Owner, 2026-09-25, after compiling the .iss four times in one day to ship
Windows fixes: "i dont want to have to keep on compiling the iss and
reshipping it, the setup exe should call the requirements from the
requirements and bat file for windows."

So the .exe installs the Visual C++ runtime, puts portable Git where the
launcher looks for it, checks out `main` from the repo the in-app updater
pulls from, and runs "Setup & Start FOREX.bat", which installs Python,
requirements.txt and starts the app. An app change never needs a new .exe,
and every install is a git checkout from the start, so it never has to be
matched to a commit (the thing that kept v6.11 "Not linked").

Inno Setup is Windows-only and cannot run here; these read the script.
"""
from __future__ import annotations

import re
from pathlib import Path

from backend.src.services.positions import core_app_update as upd

REPO = Path(__file__).resolve().parents[2]
ISS = (REPO / "installer" / "FOREX_Trader_Setup.iss").read_text(encoding="utf-8")
BAT = (REPO / "Setup & Start FOREX.bat").read_text(encoding="utf-8")


def _define(name: str) -> str:
    match = re.search(rf'^#define\s+{name}\s+"([^"]+)"', ISS, re.M)
    assert match, f"no #define {name}"
    return match.group(1)


def _section(name: str) -> str:
    match = re.search(rf"^\[{name}\]\s*$(.*?)(?=^\[\w+\]\s*$|\Z)", ISS, re.M | re.S)
    return match.group(1) if match else ""


def test_it_ships_no_app_files():
    """A file baked into the .exe is a file that needs a new .exe to change."""
    sources = re.findall(r'^\s*Source:\s*"([^"]+)"', ISS, re.M)
    assert sources == [], f"the installer still packages {sources}"


def test_it_fetches_from_the_repo_and_branch_the_updater_pulls_from():
    """Two sources of truth for where the app comes from would let an install
    start on one repository and update from another."""
    assert _define("RepoUrl") == f"{upd._GITHUB_REPO_URL}.git"
    assert _define("Branch") == upd._BRANCH


def test_it_checks_the_app_out_rather_than_copying_it():
    code = _section("Code")
    assert "fetch --depth=1 origin {#Branch}" in code
    assert "checkout -f -B {#Branch} --track origin/{#Branch}" in code


def test_it_uses_the_same_portable_git_as_the_launcher():
    """Same build, same folder: the launcher then finds the git this installer
    used instead of downloading a second one."""
    bat_url = re.search(r'set "_GIT_URL=([^"]+)"', BAT).group(1)
    assert _define("PortableGitUrl") == bat_url
    assert r"{localappdata}\Programs\PortableGit" in ISS
    assert r"%LOCALAPPDATA%\Programs\PortableGit" in BAT


def test_it_hands_over_to_the_launcher_which_installs_the_requirements():
    assert r'Filename: "{app}\Setup & Start FOREX.bat"' in _section("Run")
    assert 'set "REQ_FILE=%SCRIPT_DIR%requirements.txt"' in BAT
    assert 'pip install --quiet --upgrade -r "%REQ_FILE%"' in BAT


def test_its_version_is_its_own_not_the_apps():
    """Tying AppVersion to VERSION forced a recompile for every release."""
    assert re.search(r"^AppVersion\s*=\s*\{#InstallerVersion\}\s*$", ISS, re.M)


def test_it_still_installs_the_runtime_lightgbm_needs():
    """bugs/066: a bare Windows image crash-looped without it."""
    assert "aka.ms/vs/17/release/vc_redist.x64.exe" in _section("Code")


def test_the_uninstaller_can_only_ever_delete_its_own_folder():
    """The app files come from git, so the uninstaller removes {app} whole.
    That is only safe if {app} can never be a folder the user chose."""
    assert re.search(r'Type:\s*filesandordirs;\s*Name:\s*"\{app\}"', _section("UninstallDelete"))
    assert re.search(r"^DisableDirPage\s*=\s*yes\s*$", ISS, re.M)
    assert re.search(r"^UsePreviousAppDir\s*=\s*no\s*$", ISS, re.M)
    assert re.search(r"^DefaultDirName\s*=\s*\{localappdata\}\\FOREX Trader\s*$", ISS, re.M)


def test_the_old_python_bootstrap_is_gone():
    """The embedded Python + install_deps.py path duplicated what the launcher
    already does, and was the part of the .exe that had to track the app."""
    assert "python_embed" not in ISS
    assert "install_deps" not in ISS
    assert not (REPO / "installer" / "install_deps.py").exists()
