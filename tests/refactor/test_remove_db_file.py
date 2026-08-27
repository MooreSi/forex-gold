"""The Windows-safe temp-database teardown.

macOS cannot exercise this: os.remove succeeds here whether or not a handle is
open, so the interesting paths never run on the machine most of this is written
on. They are driven directly instead, by making os.remove fail the way Windows
makes it fail.

Background: the first Windows CI run this repo completed produced 50 teardown
errors from PermissionError [WinError 32]. Fixing the obvious leaks left 16, in
fixtures that already close both the thread-local connection and the db
worker's -- so something the resets do not reach still holds those files.
"""
from __future__ import annotations

import os

import pytest

from tests.conftest import remove_db_file


def test_a_clean_delete_is_left_alone(tmp_path):
    p = tmp_path / "clean.db"
    p.write_text("x")
    remove_db_file(str(p))
    assert not p.exists()


def test_a_transient_lock_is_survived(monkeypatch, tmp_path):
    """The reference-cycle case: the handle is released once gc runs, so the
    retry succeeds and teardown stays quiet."""
    p = tmp_path / "locked.db"
    p.write_text("x")

    real_remove = os.remove
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError(32, "The process cannot access the file")
        return real_remove(path)

    monkeypatch.setattr(os, "remove", flaky)
    remove_db_file(str(p))
    assert not p.exists()
    assert calls["n"] >= 2, "it should have retried after collecting"


def test_a_permanent_lock_raises_with_the_connection_detail(monkeypatch, tmp_path):
    """The point of the whole helper. A bare WinError 32 costs a 25-minute CI
    round trip and teaches nothing; this has to name what is still open."""
    p = tmp_path / "stuck.db"
    p.write_text("x")

    def always_locked(path):
        raise PermissionError(32, "The process cannot access the file")

    monkeypatch.setattr(os, "remove", always_locked)

    with pytest.raises(AssertionError) as exc:
        remove_db_file(str(p))

    msg = str(exc.value)
    assert "stuck.db" in msg, "the report must name the file it could not delete"
    assert "sqlite connection" in msg, "the report must say what is still open"


def test_it_does_not_silently_swallow_the_failure(monkeypatch, tmp_path):
    """A bare `except OSError: pass` would have made CI green and left a real
    leaked connection in place. That is the outcome this must never have."""
    p = tmp_path / "stuck.db"
    p.write_text("x")
    monkeypatch.setattr(os, "remove",
                        lambda path: (_ for _ in ()).throw(PermissionError(32, "locked")))

    with pytest.raises(AssertionError):
        remove_db_file(str(p))


def test_an_unrelated_oserror_is_not_treated_as_a_lock(monkeypatch, tmp_path):
    """Only PermissionError means "a handle is open". A missing file or a bad
    path is a different bug and must not be retried into silence."""
    p = tmp_path / "gone.db"

    def not_found(path):
        raise FileNotFoundError(2, "No such file")

    monkeypatch.setattr(os, "remove", not_found)
    with pytest.raises(FileNotFoundError):
        remove_db_file(str(p))
