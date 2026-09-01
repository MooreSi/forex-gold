"""A callback defined in a loop must not read the loop's variables later.

Python closures capture by reference. A function defined inside a loop and
called *after* it sees the LAST iteration's values — so in a UI that renders one
row per item, every row's button operates on the last row.

Found on 2026-09-01 in the Pending Signals editor. `save_edit` captured the
signal id correctly:

    async def save_edit(sid=signal_id):

and none of its fourteen input widgets. Entry, stop loss, all eight targets and
the notes were read from whichever row was rendered last. So editing the first
pending signal and pressing Save wrote the THIRD signal's numbers onto it — and
`update_signal` pushes SL/TP straight through to an open trade.

Two details make it worth a gate rather than a one-off fix:

  * The correct idiom is already used in the same codebase —
    `do_partial(tid=trade_id, pl_inp=partial_lots)` in `_active_trades.py`
    captures both. So this was an oversight in one place, not a convention, and
    an oversight repeats.
  * It leaves a visible-but-misleading symptom: the "Saved" confirmation
    appears on the wrong row, which looks like a rendering glitch rather than
    data going to the wrong signal.
"""
from __future__ import annotations

import pathlib

from tools.refactor_audit import late_binding as lb

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_no_callback_reads_its_loop_variables_late():
    findings = lb.scan([REPO / "backend", REPO / "frontend"], repo_root=REPO)

    assert findings == [], (
        "these run after their loop has finished and will see the last "
        "iteration's values:\n  " + "\n  ".join(str(f) for f in findings)
        + "\n\nCapture what they need as default arguments, as "
          "_active_trades.py's do_partial(tid=..., pl_inp=...) does."
    )


def test_the_allowlist_is_short_and_every_entry_has_a_reason():
    """An allowlist is where a gate goes to die. Each entry names a closure
    that is CALLED inside its own iteration, which is the only safe case."""
    assert len(lb.ALLOWED) <= 5, "the allowlist is growing; that is the warning"
    for key, reason in lb.ALLOWED.items():
        assert "::" in key
        assert len(reason) > 20, f"{key} has no real reason recorded"


class TestTheScannerWorks:
    """Negative controls. This gate reads clean, which is exactly when it is
    worth proving it can still see something."""

    def _scan(self, tmp_path, source):
        (tmp_path / "page.py").write_text(source, encoding="utf-8")
        return lb.scan([tmp_path], repo_root=tmp_path)

    def test_it_catches_the_real_shape(self, tmp_path):
        hits = self._scan(tmp_path,
                          "def build(rows):\n"
                          "    for row in rows:\n"
                          "        field = make_input()\n"
                          "        def save():\n"
                          "            send(field.value)\n"
                          "        button(on_click=save)\n")

        assert [h.names for h in hits] == [("field",)]

    def test_it_catches_the_loop_variable_itself(self, tmp_path):
        hits = self._scan(tmp_path,
                          "def build(rows):\n"
                          "    for row in rows:\n"
                          "        def save():\n"
                          "            send(row)\n"
                          "        button(on_click=save)\n")

        assert [h.names for h in hits] == [("row",)]

    def test_capturing_as_a_default_is_the_fix(self, tmp_path):
        hits = self._scan(tmp_path,
                          "def build(rows):\n"
                          "    for row in rows:\n"
                          "        field = make_input()\n"
                          "        def save(row=row, field=field):\n"
                          "            send(row, field.value)\n"
                          "        button(on_click=save)\n")

        assert hits == []

    def test_a_name_assigned_inside_the_callback_is_not_a_capture(self, tmp_path):
        """`result = await ...` inside the callback is a local, and reporting
        it would bury the real findings."""
        hits = self._scan(tmp_path,
                          "def build(rows):\n"
                          "    for row in rows:\n"
                          "        result = None\n"
                          "        def save(row=row):\n"
                          "            result = compute()\n"
                          "            send(result)\n"
                          "        button(on_click=save)\n")

        assert hits == []

    def test_a_name_from_OUTSIDE_the_loop_is_not_a_capture(self, tmp_path):
        hits = self._scan(tmp_path,
                          "def build(rows, engine):\n"
                          "    for row in rows:\n"
                          "        def save(row=row):\n"
                          "            engine.send(row)\n"
                          "        button(on_click=save)\n")

        assert hits == []

    def test_a_lambda_is_checked_as_well(self, tmp_path):
        hits = self._scan(tmp_path,
                          "def build(rows):\n"
                          "    for row in rows:\n"
                          "        button(on_click=lambda: send(row))\n")

        assert [h.func for h in hits] == ["<lambda>"]

    def test_a_syntax_error_is_skipped_not_crashed_on(self, tmp_path):
        assert self._scan(tmp_path, "def f(:\n") == []


class TestThePendingSignalsEditorSpecifically:
    """The bug that prompted this. Named so a future edit that drops the
    captures fails against something explicit rather than only the sweep."""

    def test_save_edit_captures_every_widget_it_reads(self):
        import ast

        src = (REPO / "frontend/pages/trading/_pending_signals.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "save_edit")

        captured = {a.arg for a in fn.args.args + fn.args.kwonlyargs}

        for widget in ("e_dir", "e_el", "e_eh", "e_sl", "e_notes", "e_result",
                       *(f"e_tp{n}" for n in range(1, 9))):
            assert widget in captured, (
                f"{widget} is read from the enclosing loop, so Save on one row "
                f"would use another row's value"
            )
        assert "sid" in captured
