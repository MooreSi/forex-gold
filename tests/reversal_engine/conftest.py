"""Shared fixtures for the Reversal Engine's tests.

`fresh_re_db` opens a throwaway `reversal_engine.db` through
`reversal_engine_repo`. Its own name, not `fresh_db`: the canonical
`fresh_db` in tests/conftest.py opens the core database, and many files in
this package still define a local `fresh_db` of this shape
(tests/refactor/test_fixture_dedup.py counts those and only lets them
shrink). New tests here use this one instead of adding another copy.
"""
import os
import tempfile

import pytest

from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
from tests.conftest import remove_db_file


@pytest.fixture
def fresh_re_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_repo.init(path)
    yield re_repo
    re_repo.close_db()
    remove_db_file(path)
