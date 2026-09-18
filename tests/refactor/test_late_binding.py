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


# TestThePendingSignalsEditorSpecifically was deleted on 2026-09-18.
#
# It pinned that `save_edit` in `frontend/pages/trading/_pending_signals.py`
# captured all fifteen of its row widgets as default arguments, because a
# NiceGUI callback defined in a loop reads the loop's variables when it fires —
# so without the captures, Save on one row wrote another row's values.
#
# Both the file and the mechanism are gone: the React port deleted the editor,
# and a React handler closes over the props of the row that rendered it, so the
# Python default-argument idiom has no equivalent to check. The generic sweep
# above still guards every place the mechanism DOES still exist, which is now
# `backend/` only.
#
# **The requirement is not gone.** React has its own version of this bug — a
# handler that reads state captured on an earlier render, so a row's button
# acts on the wrong row. When the pending-signals editor is rebuilt (task 080),
# it needs a test that edits one row of several and asserts the OTHER rows are
# untouched. That is recorded in
# docs/todo/frontend/react-port/080-remaining-tabs.md; it is not covered by
# anything today, and saying so is the point of this comment.

