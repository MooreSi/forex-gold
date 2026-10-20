"""Storage for the Calendar's day journal (docs/todo/023, #8).

In reversal_engine.db, which is one file whatever the account, with
`account_env` as a column -- the `decision_log_repo` pattern. The core
database is per-environment, so a journal kept there would split in half the
day the account switches; here demo and live are separated when the question
is asked (provisional, docs/simon-handover/055).
"""
from __future__ import annotations

import json
import time

from backend.src.services.reversal_engine import reversal_engine_repo as re_db

_SCHEMA = """
CREATE TABLE IF NOT EXISTS day_journal (
    account_env TEXT NOT NULL,
    day         TEXT NOT NULL,
    note        TEXT NOT NULL,
    tags        TEXT NOT NULL,
    mood        INTEGER,
    updated_at  REAL NOT NULL,
    PRIMARY KEY (account_env, day)
);
"""


def create_schema() -> None:
    re_db.get_db().exec(_SCHEMA)


def _out(row) -> dict:
    d = dict(row)
    d.pop("account_env", None)
    d["tags"] = json.loads(d["tags"])
    return d


def between(env: str, start: str, end: str) -> list[dict]:
    create_schema()   # a reader on a fresh install, before the first note
    return [_out(r) for r in re_db.get_db().all(
        "SELECT * FROM day_journal WHERE account_env=? AND day>=? AND day<=? ORDER BY day",
        env, start, end)]


def upsert(env: str, day: str, note: str, tags: list[str], mood: int | None) -> dict:
    create_schema()
    db = re_db.get_db()
    db.run(
        "INSERT INTO day_journal (account_env, day, note, tags, mood, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(account_env, day) DO UPDATE SET "
        "note=excluded.note, tags=excluded.tags, mood=excluded.mood, "
        "updated_at=excluded.updated_at",
        env, day, note, json.dumps(tags), mood, time.time(),
    )
    return _out(db.get("SELECT * FROM day_journal WHERE account_env=? AND day=?", env, day))


def delete(env: str, day: str) -> None:
    create_schema()
    re_db.get_db().run("DELETE FROM day_journal WHERE account_env=? AND day=?", env, day)
