"""Shared fixtures for the Bounce engine's tests.

`fresh_db` here is the Bounce variant of the canonical one: it opens
`test_signal_repo` rather than the main `db` module, so it cannot simply
inherit `tests/conftest.py`'s. It lives in a conftest rather than being copied
into each test file for the reason `tests/refactor/test_fixture_dedup.py`
exists -- `fresh_db` was once defined in 114 files, and every change to the DB
layer then broke dozens at once.

The close before the unlink is the Windows hazard, not tidiness: the repo's
adapter holds the file open, POSIX lets you unlink a file with a live handle
and Windows does not. That exact pair produced 50 teardown errors on the first
Windows CI run this repo completed.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from backend.src.services.test_signal import test_signal_repo as _ts_db
from tests.conftest import remove_db_file


@pytest.fixture
def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    _ts_db.init(path)
    yield _ts_db
    _ts_db.close_db()
    remove_db_file(path)
