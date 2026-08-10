"""pyproject.toml must be buildable and honest about its dependencies.

Testing review 2026-08-08, finding H3: the build backend was
`setuptools.backends.legacy:build` (a module that does not exist), and the
declared dependencies omitted ~12 packages that requirements.txt requires, so
`pip install .` produced an app that couldn't import half its own code.
"""
from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = REPO_ROOT / "pyproject.toml"
REQUIREMENTS = REPO_ROOT / "requirements.txt"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _dist_name(spec: str) -> str:
    """'scikit-learn>=1.3.0; sys_platform==...' -> 'scikit-learn' (normalised)."""
    spec = spec.split(";", 1)[0].strip()
    for sep in ("==", ">=", "<=", "~=", ">", "<", "!=", "="):
        if sep in spec:
            spec = spec.split(sep, 1)[0]
            break
    return spec.strip().lower().replace("_", "-")


def test_build_backend_is_importable():
    backend = _pyproject()["build-system"]["build-backend"]
    module = backend.split(":", 1)[0]
    # An unimportable backend fails every `pip install`/wheel build silently
    # until someone actually tries to package the app.
    importlib.import_module(module)


def test_runtime_deps_cover_requirements():
    declared = {_dist_name(d) for d in _pyproject()["project"]["dependencies"]}
    required = {
        _dist_name(line)
        for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    missing = required - declared
    assert not missing, f"pyproject omits required deps: {sorted(missing)}"
