"""Read a module's source whether it is still a file or has become a package.

A module that outgrows the 800-line ceiling becomes a package in the same
place, keeping its import path (docs/system/rules/70-file-organisation.md).
Tests that read "<name>.py" off disk then fail on a change that did not touch
their subject at all -- which is noise, and worse, it is noise that arrives
during a refactor when a real failure most needs to stand out.
"""
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def module_source(rel: str) -> str:
    """Full source of *rel* (e.g. "frontend/app.py"), module or package."""
    path = REPO / rel
    if path.exists():
        return path.read_text(encoding="utf-8")
    package = path.with_suffix("")
    if not package.is_dir():
        raise FileNotFoundError(f"{rel}: neither a module nor a package")
    return "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(package.rglob("*.py"))
    )
