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


def test_the_reference_repos_are_only_partly_clean():
    """The three migrated engines are held up as the pattern for the other 16
    domains. They are not uniformly transactional, and that matters because
    whatever is wrong here gets copied fifteen more times.

    Verified rather than assumed:
      reversal_engine_repo.py  clean
      breakout_signal_repo.py  init
      test_signal_repo.py      insert_signal, log_analysis

    test_signal_repo.insert_signal is the substantive one: it INSERTs a signal
    and then issues a separate `UPDATE test_signals SET signal_ref=?` to stamp
    the reference derived from the new rowid. Two unwrapped writes -- a crash
    between them leaves a signal row with a NULL signal_ref.

    Tighten this test as those are fixed; it is a to-do list, not a licence.
    """
    offenders = sg.transaction_report()
    assert "forex_trader/reversal_engine/reversal_engine_repo.py" not in offenders
    assert offenders.get("forex_trader/test_signal/test_signal_repo.py") == [
        "insert_signal", "log_analysis"]


def test_ui_db_gate_sees_the_aliased_import_form():
    """`from forex_trader.core import database as db_module` is how the pages
    actually do it -- the module path contains no "database" component, so a
    naive check reports 5 files instead of 14."""
    report = sg.ui_db_report()
    assert len(report) > 10, f"only found {len(report)}; the alias form is being missed"
