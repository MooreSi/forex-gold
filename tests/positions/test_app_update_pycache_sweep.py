"""The post-update pycache sweep: repo code only, and it cannot cost the EA deploy.

VPS, 2026-09-26 22:28:08: "[Update] pip install / cache clear step failed:
[WinError 3] ... .venv\\Lib\\site-packages\\backports\\tarfile\\__pycache__".
Two things were wrong. The sweep walked the whole of `.venv` -- thousands of
third-party caches that a `git checkout` never makes stale -- and a directory
vanishing under the walk raised out of it. And the three post-update steps
shared one try, so the sweep's exception skipped the EA deploy after it.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.broker import ea_deploy
from backend.src.services.positions import core_app_update as upd


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for rel in ("backend/src/__pycache__", "backend/src/x/__pycache__",
                ".venv/Lib/site-packages/pkg/__pycache__"):
        (tmp_path / rel).mkdir(parents=True)
        (tmp_path / rel / "m.cpython-313.pyc").write_bytes(b"x")
    monkeypatch.setattr(upd, "_REPO_ROOT", tmp_path)
    return tmp_path


def test_the_sweep_clears_the_repo_s_caches_and_leaves_the_venv_alone(repo):
    upd._clear_pycache()

    assert not (repo / "backend/src/__pycache__").exists()
    assert not (repo / "backend/src/x/__pycache__").exists()
    assert (repo / ".venv/Lib/site-packages/pkg/__pycache__").exists()


def test_a_cache_dir_vanishing_mid_sweep_is_not_an_error(repo, monkeypatch):
    real_rmtree = upd.shutil.rmtree

    def _rmtree_racing_another_sweep(path, *a, **k):
        real_rmtree(repo / "backend/src/x", ignore_errors=True)
        return real_rmtree(path, *a, **k)
    monkeypatch.setattr(upd.shutil, "rmtree", _rmtree_racing_another_sweep)

    upd._clear_pycache()

    assert not (repo / "backend/src/__pycache__").exists()


def test_a_failed_sweep_still_deploys_the_ea(monkeypatch):
    async def _run_git(*args, **kw):
        return (0, "", "")
    monkeypatch.setattr(upd, "_run_git", _run_git)
    monkeypatch.setattr(upd.Path, "exists", lambda self: True)
    monkeypatch.setattr(upd, "_restart", lambda: None)
    monkeypatch.setattr(upd.subprocess, "run", lambda *a, **k: None)

    def _boom():
        raise FileNotFoundError("[WinError 3] The system cannot find the path specified")
    monkeypatch.setattr(upd, "_clear_pycache", _boom)
    deploys: list = []
    monkeypatch.setattr(ea_deploy, "deploy_after_update",
                        lambda *a, **k: deploys.append(True) or {"ok": True})

    result = asyncio.run(upd.apply_update())

    assert deploys == [True]
    assert result["ok"] is True
    assert "WinError 3" in (result["error"] or "")
