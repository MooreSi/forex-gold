"""`--update-baseline` must not delete the reasons the numbers are what they are.

The baseline file carries more than numbers. `_raised` records four LOC
entries that went UP on 2026-08-27 with the owner's explicit sign-off, one of
them flagged in its own text as the least comfortable of the four; `_tightened`
records three that came down and why. CLAUDE.md and the file's own header both
point at `_raised` as the thing that distinguishes a signed-off exception from
a regression: "a rise with no entry there is a regression, not a decision".

`--update-baseline` rebuilt the file from a literal, so running the documented
command silently deleted all of it. Found 2026-09-01 by running it.

Nothing here writes to the real baseline file.
"""
from __future__ import annotations

import json

import pytest

from tools.refactor_audit import structure_gates as sg


@pytest.fixture
def baseline_file(tmp_path, monkeypatch):
    path = tmp_path / "structure_baseline.json"
    path.write_text(json.dumps({
        "_comment": ["the standing rule"],
        "_raised": {"_when": "2026-08-27", "some/file.py": {"was": 1, "now": 2}},
        "_tightened": {"_when": "2026-08-27", "_why": ["found while checking"]},
        "baseline": {"loc": {"some/file.py": 2}, "sql": {},
                     "transaction": {}, "ui_db": {}},
    }, indent=2), encoding="utf-8")
    monkeypatch.setattr(sg, "BASELINE_PATH", path)
    return path


def _regenerate(monkeypatch, measured, root):
    """Run the documented regenerate command against a throwaway baseline.

    REPO_ROOT moves with it because the command prints the path it wrote
    relative to the repo, and a tmp_path is not inside this one.
    """
    monkeypatch.setattr(sg, "current", lambda: measured)
    monkeypatch.setattr(sg.od, "REPO_ROOT", root)
    assert sg.main(["--update-baseline"]) == 0


class TestTheRecordSurvives:
    def test_the_signed_off_raises_are_kept(self, baseline_file, monkeypatch):
        _regenerate(monkeypatch, {"loc": {"some/file.py": 1}, "sql": {},
                                  "transaction": {}, "ui_db": {}},
                    baseline_file.parent)

        written = json.loads(baseline_file.read_text(encoding="utf-8"))

        assert written["_raised"]["some/file.py"] == {"was": 1, "now": 2}
        assert written["_raised"]["_when"] == "2026-08-27"

    def test_the_tightening_record_is_kept(self, baseline_file, monkeypatch):
        _regenerate(monkeypatch, {"loc": {}, "sql": {},
                                  "transaction": {}, "ui_db": {}},
                    baseline_file.parent)

        written = json.loads(baseline_file.read_text(encoding="utf-8"))

        assert written["_tightened"]["_why"] == ["found while checking"]

    def test_any_underscore_key_is_kept(self, baseline_file, monkeypatch):
        """Not an allowlist of the two that exist today. The convention is the
        leading underscore, and the next note someone adds must survive too."""
        data = json.loads(baseline_file.read_text(encoding="utf-8"))
        data["_a_note_added_later"] = {"why": "because"}
        baseline_file.write_text(json.dumps(data), encoding="utf-8")

        _regenerate(monkeypatch, {"loc": {}, "sql": {},
                                  "transaction": {}, "ui_db": {}},
                    baseline_file.parent)

        written = json.loads(baseline_file.read_text(encoding="utf-8"))

        assert written["_a_note_added_later"] == {"why": "because"}


class TestTheNumbersStillUpdate:
    def test_the_measured_numbers_replace_the_old_ones(self, baseline_file,
                                                        monkeypatch):
        """Negative control for everything above: a regenerator that kept the
        whole file unchanged would pass all three."""
        _regenerate(monkeypatch, {"loc": {"other.py": 9}, "sql": {},
                                  "transaction": {}, "ui_db": {}},
                    baseline_file.parent)

        written = json.loads(baseline_file.read_text(encoding="utf-8"))

        assert written["baseline"]["loc"] == {"other.py": 9}

    def test_a_file_that_dropped_below_the_ceiling_leaves_the_section(
        self, baseline_file, monkeypatch,
    ):
        """Stricter than lowering its number: a file absent from a section may
        never reappear in it."""
        _regenerate(monkeypatch, {"loc": {}, "sql": {},
                                  "transaction": {}, "ui_db": {}},
                    baseline_file.parent)

        written = json.loads(baseline_file.read_text(encoding="utf-8"))

        assert written["baseline"]["loc"] == {}




class TestAFreshFile:
    def test_it_works_when_there_is_no_baseline_yet(self, tmp_path, monkeypatch):
        """First run in a new checkout: nothing to preserve, and it must not
        fail trying."""
        path = tmp_path / "new.json"
        monkeypatch.setattr(sg, "BASELINE_PATH", path)

        _regenerate(monkeypatch, {"loc": {"a.py": 1}, "sql": {},
                                  "transaction": {}, "ui_db": {}}, tmp_path)

        written = json.loads(path.read_text(encoding="utf-8"))
        assert written["baseline"]["loc"] == {"a.py": 1}

    def test_an_unreadable_baseline_does_not_lose_the_new_numbers(
        self, tmp_path, monkeypatch,
    ):
        """A half-written or hand-mangled file must not stop the regenerate --
        there is nothing to preserve from it anyway."""
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        monkeypatch.setattr(sg, "BASELINE_PATH", path)

        _regenerate(monkeypatch, {"loc": {"a.py": 1}, "sql": {},
                                  "transaction": {}, "ui_db": {}}, tmp_path)

        assert json.loads(path.read_text(encoding="utf-8"))["baseline"]["loc"] == {"a.py": 1}


class TestTheCommentIsRecordToo:
    def test_the_existing_comment_survives_verbatim(self, baseline_file,
                                                     monkeypatch):
        """The live file's comment carries the EXCEPTION paragraph naming
        `_raised` as what separates a signed-off rise from a regression.
        Overwriting it with the tool's default deletes the sentence that
        makes `_raised` mean anything."""
        _regenerate(monkeypatch, {"loc": {}, "sql": {},
                                  "transaction": {}, "ui_db": {}},
                    baseline_file.parent)

        written = json.loads(baseline_file.read_text(encoding="utf-8"))

        assert written["_comment"] == ["the standing rule"]

    def test_a_fresh_file_gets_the_default_comment(self, tmp_path, monkeypatch):
        """Negative control: preserving must not mean never writing one."""
        path = tmp_path / "new.json"
        monkeypatch.setattr(sg, "BASELINE_PATH", path)

        _regenerate(monkeypatch, {"loc": {}, "sql": {},
                                  "transaction": {}, "ui_db": {}}, tmp_path)

        written = json.loads(path.read_text(encoding="utf-8"))

        assert any("shrink-only" in line.lower() for line in written["_comment"])
