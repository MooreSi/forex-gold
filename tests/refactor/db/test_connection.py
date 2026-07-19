"""Characterizes forex_trader.src.db.connection's module-level adapter
singleton — see docs/todo/refactor/backend-foundation/010-*.md."""
import os
import tempfile

import pytest

from forex_trader.src.db import connection


@pytest.fixture(autouse=True)
def _reset_connection():
    yield
    connection.close_db()


def test_init_db_creates_adapter_bound_to_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        adapter = connection.init_db(path)
        assert adapter is not None
        assert connection.get_db() is adapter
    finally:
        os.remove(path)


def test_get_db_raises_before_init_db_called():
    with pytest.raises(RuntimeError):
        connection.get_db()


def test_set_db_swaps_the_active_adapter():
    """set_db() exists specifically so tests can inject a fake/in-memory
    adapter without going through init_db()'s real file-path connection."""
    class _FakeAdapter:
        pass

    fake = _FakeAdapter()
    connection.set_db(fake)
    assert connection.get_db() is fake


def test_close_db_releases_the_adapter():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        connection.init_db(path)
        connection.close_db()
        with pytest.raises(RuntimeError):
            connection.get_db()
    finally:
        os.remove(path)
