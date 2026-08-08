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
           "ui_db": {"frontend/pages/new_page.py": 1}}
    assert len(sg.check(now, baseline)) == 1


def test_engine_py_is_in_the_loc_baseline():
    """It is 3,143 lines and regrew after the last refactor was called done.

    The file is now backend/src/runtime.py -- same content, relocated in the
    finale move -- and the shrink-only ceiling must follow it, not lapse.
    """
    assert "backend/src/runtime.py" in sg.load_baseline()["loc"]


def test_repo_files_are_exempt_from_the_sql_gate():
    """SQL belongs in a repo; that is the whole point."""
    assert sg.is_repo_file(sg.Path("forex_trader/reversal_engine/reversal_engine_repo.py"))
    assert sg.is_repo_file(sg.Path("forex_trader/core/core_db_channel.py"))
    assert not sg.is_repo_file(sg.Path("frontend/pages/history.py"))


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
    """`from backend.src.db import database as db_module` is how the pages
    do it -- the module path contains no "database" component, so a naive
    check misses it. The original assertion pinned "more than 10 files",
    which stopped being true the moment M3 started draining pages; what the
    gate must actually guarantee is that EVERY frontend file still using
    the alias form is reported -- down to zero when the drain completes."""
    report = sg.ui_db_report()
    offenders = set()
    for path in sg.od.production_files():
        if "frontend" not in path.parts or path.suffix != ".py":
            continue
        text = path.read_text(encoding="utf-8")
        if "import database" in text or "import sqlite3" in text:
            offenders.add(path.relative_to(sg.od.REPO_ROOT).as_posix())
    missed = offenders - set(report)
    assert not missed, f"alias-form importers missed by the gate: {missed}"


# ── Controller gates ────────────────────────────────────────────────────────
# Both are enforced at zero, so unlike the ratchets above there is no baseline
# to compare against -- the assertion is simply that the layer is clean, plus
# a negative control proving each gate can see a breach.

def test_no_controller_exceeds_the_controller_line_ceiling():
    """A controller that routes and does not decide never gets long.

    Crossing this means logic moved back up into the translation layer, which
    is what produced a 403-line history controller full of ledger merges.
    """
    over = sg.controller_loc_report()
    assert over == {}, (
        f"controllers over {sg.CONTROLLER_LOC_CEILING} lines: {over}")


def test_every_controller_is_a_flat_module():
    offenders = sg.controller_shape_report()
    assert offenders == [], f"non-flat controllers: {offenders}"


def test_the_controller_loc_gate_can_see_an_oversized_file(tmp_path, monkeypatch):
    """Negative control. A gate that has never been red has proved nothing."""
    fake_repo = tmp_path
    (fake_repo / sg.CONTROLLER_DIR).mkdir(parents=True)
    long_file = fake_repo / sg.CONTROLLER_DIR / "fat_controller.py"
    long_file.write_text("# pad\n" * (sg.CONTROLLER_LOC_CEILING + 1), encoding="utf-8")
    monkeypatch.setattr(sg.od, "REPO_ROOT", fake_repo)
    report = sg.controller_loc_report()
    assert report, "gate did not flag a file over the ceiling"
    assert any(k.endswith("fat_controller.py") for k in report)


def test_the_controller_shape_gate_can_see_a_package_and_a_bad_name(tmp_path, monkeypatch):
    """Negative control for both shapes the gate rejects.

    A package directory under controllers/ is how remote/ and sync/ grew to
    4,950 lines of websocket server inside the controller layer.
    """
    fake_repo = tmp_path
    base = fake_repo / sg.CONTROLLER_DIR
    (base / "nested").mkdir(parents=True)
    (base / "nested" / "__init__.py").write_text("x = 1\n", encoding="utf-8")
    (base / "helpers.py").write_text("x = 1\n", encoding="utf-8")
    (base / "chart_controller.py").write_text("x = 1\n", encoding="utf-8")
    (base / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(sg.od, "REPO_ROOT", fake_repo)
    offenders = " ".join(sg.controller_shape_report())
    assert "nested" in offenders, "package directory not flagged"
    assert "helpers.py" in offenders, "non-*_controller.py module not flagged"
    assert "chart_controller.py" not in offenders, "flat controller wrongly flagged"
