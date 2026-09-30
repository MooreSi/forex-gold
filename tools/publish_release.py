"""Publish a GitHub release for the app version in version_history.py.

Run by .github/workflows/release.yml on every push to main that touches the
version file or the changelog. Idempotent: if a release for the current
version already exists it does nothing, so an ordinary push never re-releases.

The current version is the FIRST entry of RELEASES (the file says so). The
release body is that version's section of CHANGELOG.md, headed like the
earlier releases; without a section it falls back to the About-screen bullets.

    python -m tools.publish_release <commit-sha>
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parents[1]
VERSION_FILE = REPO / "backend" / "src" / "utils" / "version_history.py"
CHANGELOG = REPO / "CHANGELOG.md"
CHANGELOG_URL = "https://github.com/MooreSi/forex-gold/blob/main/CHANGELOG.md"


def current_release(path: Path = VERSION_FILE) -> tuple[str, str, list]:
    """(tag, title, bullets) of RELEASES[0], loaded from the file alone so the
    workflow needs no app dependencies."""
    spec = importlib.util.spec_from_file_location("_version_history", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    tag, title, _colour, _label, bullets = mod.RELEASES[0]
    return tag, title, list(bullets)


def changelog_section(text: str, tag: str) -> Optional[str]:
    """The body under `## <tag> ...`, up to the next `## `; None if absent."""
    m = re.search(rf"^## {re.escape(tag)}(?=[ \t—-]).*\n", text, re.M)
    if not m:
        return None
    rest = text[m.end():]
    nxt = re.search(r"^## ", rest, re.M)
    return (rest[:nxt.start()] if nxt else rest).strip("\n") + "\n"


def release_body(tag: str, title: str, bullets: list, changelog: str) -> str:
    head = f"## {tag} — {title}\n\n"
    section = changelog_section(changelog, tag)
    if section is not None:
        return (head + f"Full engineering record in [CHANGELOG.md]({CHANGELOG_URL}).\n\n"
                + section)
    return head + "\n".join(f"- {b}" for b in bullets) + "\n"


def _gh(*args: str) -> int:
    return subprocess.run(["gh", *args], check=False).returncode


def publish(tag: str, title: str, body: str, sha: str) -> str:
    if _gh("release", "view", tag) == 0:
        return "exists"
    rc = _gh("release", "create", tag, "--title", f"{tag} — {title}",
             "--notes", body, "--target", sha, "--latest")
    if rc != 0:
        raise RuntimeError(f"gh release create {tag} failed (exit {rc})")
    return "created"


def main(argv: list) -> int:
    sha = argv[1] if len(argv) > 1 else "main"
    tag, title, bullets = current_release()
    changelog = CHANGELOG.read_text(encoding="utf-8") if CHANGELOG.exists() else ""
    print(f"{tag}: {publish(tag, title, release_body(tag, title, bullets, changelog), sha)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
