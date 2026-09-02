"""The activation screen needs a working database, because it does real work.

Second half of this morning's lockout. Once the admin server started behind
the activation screen, the registration arrived and then:

    [RemoteServer] Registration request from MacMini.localdomain (192.168.0.53)
    [RemoteServer] Registration Telegram notify failed: no such table: telegram_config

The alert WAS firing. It could not read the Telegram settings because the
database had never been opened: `run.py` calls `_licence_enforce()` before
`_db_mod.init()`, and the activation screen never returns, so on that path the
schema is never created.

The consequence is the same lockout in a subtler form. The screen tells the
owner "awaiting administrator approval" and the notification that would let
him approve it cannot be sent -- so he waits for a message that will never
arrive, with nothing on screen saying why.

The database is now opened before the licence check. It is the owner's own
database either way, and the guard reads a file in the home directory rather
than anything in it -- so nothing about the licence decision changes, only
whether the screen behind it can function.
"""
from __future__ import annotations

import pathlib
import re

import pytest

RUN_PY = pathlib.Path("run.py").read_text(encoding="utf-8")


def _index_of_call(needle: str) -> int:
    """Position of a CALL, not a definition.

    `_start_mt5_bridge` and friends are defined near the top of run.py, so
    matching a bare name finds the `def` and every ordering assertion inverts.
    """
    m = re.search(r"^\s+" + re.escape(needle), RUN_PY, re.M)
    assert m, f"{needle!r} not found as a call in run.py"
    return m.start()


class TestTheOrdering:
    def test_the_database_is_opened_before_the_licence_check(self):
        """The whole point. Everything the activation screen does -- the
        Telegram notification most of all -- reads this database."""
        assert _index_of_call("_db_mod.init(") < _index_of_call("_licence_enforce()")

    def test_the_config_is_loaded_before_the_database(self):
        """init() needs the resolved db_path, which depends on account_env."""
        assert _index_of_call("cfg  = cfg_module.load()") < _index_of_call("_db_mod.init(")

    def test_the_licence_check_still_runs_before_the_engines(self):
        """The rule that must NOT change: nothing that trades starts before
        the licence is verified."""
        assert _index_of_call("_licence_enforce()") < _index_of_call(
            "bridge_proc = _start_mt5_bridge()")


class TestTheTelegramConfigIsReachable:
    def test_the_table_the_notify_reads_is_created_by_init(self, fresh_db):
        """`no such table: telegram_config` was the actual error. If the
        schema stops creating it, the registration alert breaks again in
        exactly the same way and the owner waits for a message that never
        comes."""
        from backend.src.db import database as db

        with db.db() as conn:
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name='telegram_config'").fetchone()

        assert row is not None

    def test_the_alert_path_can_read_it(self, fresh_db):
        """One level up from the table existing: the function the registration
        notify actually calls must return without raising."""
        from backend.src.db import database as db

        assert isinstance(db.get_telegram_config(), dict)


class TestTheRegressionItself:
    def test_a_registration_notify_survives_an_unopened_database(self, monkeypatch):
        """Belt and braces. The ordering above is the fix, but this path runs
        on a screen that is the only way back into the app -- so a database
        problem must cost the notification, not the registration.
        """
        import asyncio

        from backend.src.services.cluster.remote import server as rs

        async def _boom(*a, **kw):
            raise RuntimeError("no such table: telegram_config")
        monkeypatch.setattr(
            "backend.src.services.telegram.alerts.send_message", _boom)

        asyncio.run(rs._notify_new_registration("h", "e", "n", "i", token="abcdef01"))
