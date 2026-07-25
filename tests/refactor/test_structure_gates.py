"""A ratchet that cannot fail is decoration.

The previous refactor was declared complete and then engine.py regrew by 171
lines. These tests exist to prove the gates would actually catch that, rather
than reporting comfortable numbers nobody checks.
"""
from __future__ import annotations

from tools.refactor_audit import structure_gates as sg


def test_the_baseline_matches_reality():
    """A drifted baseline silently disables every gate."""
    assert sg.check(sg.current(), sg.load_baseline()) == []


def test_growth_beyond_the_baseline_fails():
    baseline = {"loc": {"a.py": 900}, "sql": {}, "transaction": {}, "ui_db": {}}
    now = {"loc": {"a.py": 901}, "sql": {}, "transaction": {}, "ui_db": {}}
    failures = sg.check(now, baseline)
    assert len(failures) == 1
    assert "900 -> 901" in failures[0]


def test_shrinking_passes():
    baseline = {"loc": {"a.py": 900}, "sql": {}, "transaction": {}, "ui_db": {}}
    now = {"loc": {"a.py": 400}, "sql": {}, "transaction": {}, "ui_db": {}}
    assert sg.check(now, baseline) == []


def test_a_brand_new_violation_fails():
    """The regrowth case: a file crossing 800 for the first time."""
    baseline = {"loc": {}, "sql": {}, "transaction": {}, "ui_db": {}}
    now = {"loc": {"new.py": 801}, "sql": {}, "transaction": {}, "ui_db": {}}
    failures = sg.check(now, baseline)
    assert len(failures) == 1
    assert "new violation" in failures[0]


def test_a_new_ui_database_import_fails():
    """The frontend/backend boundary, enforced in today's layout."""
    baseline = {"loc": {}, "sql": {}, "transaction": {}, "ui_db": {}}
    now = {"loc": {}, "sql": {}, "transaction": {},
           "ui_db": {"forex_trader/ui/pages/new_page.py": 1}}
    assert len(sg.check(now, baseline)) == 1


def test_engine_py_is_in_the_loc_baseline():
    """It is 3,165 lines and regrew after the last refactor was called done."""
    assert "forex_trader/core/engine.py" in sg.load_baseline()["loc"]


def test_repo_files_are_exempt_from_the_sql_gate():
    """SQL belongs in a repo; that is the whole point."""
    assert sg.is_repo_file(sg.Path("forex_trader/reversal_engine/reversal_engine_repo.py"))
    assert sg.is_repo_file(sg.Path("forex_trader/core/core_db_channel.py"))
    assert not sg.is_repo_file(sg.Path("forex_trader/ui/pages/history.py"))


def test_the_reference_repos_are_transactionally_clean():
    """The three migrated engines are the pattern for the other 16 domains, so
    anything wrong here gets copied fifteen more times.

    They are clean now. `test_signal_repo.insert_signal` was not: it INSERTed a
    signal and then issued a separate `UPDATE test_signals SET signal_ref=?` to
    stamp the reference derived from the new rowid, so a crash between the two
    committed a row with a NULL signal_ref. Now one transaction.
    """
    offenders = sg.transaction_report()
    for repo in ("forex_trader/reversal_engine/reversal_engine_repo.py",
                 "forex_trader/breakout_signal/breakout_signal_repo.py",
                 "forex_trader/test_signal/test_signal_repo.py"):
        assert repo not in offenders, f"{repo} is the pattern others copy"


def test_ddl_is_not_counted_as_a_write():
    """Schema and migrate-on-write statements are not business writes.

    Counting them reported two false positives against one real defect, and a
    gate that is wrong two thirds of the time gets switched off.
    """
    import ast
    fn = ast.parse(
        "def f():\n"
        "    get_db().run('CREATE TABLE t (id INT)')\n"
        "    get_db().run(f'ALTER TABLE t ADD COLUMN {c} {d}')\n"
        "    get_db().run('INSERT INTO t VALUES (?)', 1)\n"
    ).body[0]
    assert sg._writes_in(fn) == 1


def test_an_unreadable_statement_still_counts():
    """A run(sql_var) we cannot inspect must not be assumed harmless."""
    import ast
    fn = ast.parse(
        "def f():\n"
        "    get_db().run(some_sql, 1)\n"
        "    get_db().run(other_sql, 2)\n"
    ).body[0]
    assert sg._writes_in(fn) == 2


def test_ui_db_gate_sees_the_aliased_import_form():
    """`from forex_trader.core import database as db_module` is how the pages
    actually do it -- the module path contains no "database" component, so a
    naive check reports 5 files instead of 14."""
    report = sg.ui_db_report()
    assert len(report) > 10, f"only found {len(report)}; the alias form is being missed"
