"""An `async def` called without `await` builds a coroutine and drops it.

No exception, no work done — a `RuntimeWarning` on stderr that nobody reads in
a running app, and the operation simply never happens.

The way it gets introduced is not by writing a bug, it is by changing a
function from sync to async: every `await`ed call site keeps working, and any
bare call site silently stops. That happened here on 2026-09-01. Four timer
callbacks were made `async def` so they would stop doing SQLite reads on the
event loop, and one of them also had a one-off call to populate its label on
first render. That call became a no-op, and the circuit-breaker badge in the
header showed nothing for the first five seconds after every page load.

Small consequence, but the class is not small: the same edit to a callback that
placed an order, or moved a stop, would have failed the same way and just as
quietly.

Only bare NAME calls are checked. `app.shutdown()` is NiceGUI's own,
synchronous, and shares a name with three async `shutdown` methods here —
matching attribute calls too would report it and two others as false positives,
and a gate that cries wolf gets switched off.
"""
from __future__ import annotations

import pathlib

from tools.refactor_audit import unawaited_coroutines as uc

REPO = pathlib.Path(__file__).resolve().parents[2]
ROOTS = ["backend", "frontend", "tools", "run.py"]


def test_no_coroutine_is_created_and_dropped():
    findings = uc.scan([REPO / r for r in ROOTS])

    assert findings == [], (
        "these calls build a coroutine and throw it away, so the work never "
        "happens:\n  " + "\n  ".join(
            str(f).replace(str(REPO) + "/", "") for f in findings)
        + "\n\nEither await it, or hand it to the event loop "
          "(asyncio.create_task / ui.timer(..., once=True))."
    )


class TestTheScannerWorks:
    """Negative controls. A scanner that found nothing would satisfy the gate
    above permanently, which is the failure this repo was rebuilt after."""

    def _scan(self, tmp_path, source, name="sample.py"):
        (tmp_path / name).write_text(source, encoding="utf-8")
        return uc.scan([tmp_path])

    def test_it_catches_the_real_shape(self, tmp_path):
        """Exactly what happened: sync callback made async, bare call left."""
        hits = self._scan(tmp_path,
                          "async def refresh():\n"
                          "    pass\n"
                          "def build():\n"
                          "    refresh()\n")

        assert [h.name for h in hits] == ["refresh"]

    def test_an_awaited_call_is_fine(self, tmp_path):
        hits = self._scan(tmp_path,
                          "async def refresh():\n"
                          "    pass\n"
                          "async def build():\n"
                          "    await refresh()\n")

        assert hits == []

    def test_handing_it_to_the_loop_is_fine(self, tmp_path):
        hits = self._scan(tmp_path,
                          "import asyncio\n"
                          "async def refresh():\n"
                          "    pass\n"
                          "def build():\n"
                          "    asyncio.create_task(refresh())\n")

        assert hits == []

    def test_passing_it_as_a_callback_is_fine(self, tmp_path):
        """`ui.timer(5.0, refresh)` passes the function, it does not call it."""
        hits = self._scan(tmp_path,
                          "async def refresh():\n"
                          "    pass\n"
                          "def build():\n"
                          "    ui.timer(5.0, refresh)\n")

        assert hits == []

    def test_a_plain_sync_call_is_fine(self, tmp_path):
        hits = self._scan(tmp_path,
                          "def refresh():\n"
                          "    pass\n"
                          "def build():\n"
                          "    refresh()\n")

        assert hits == []

    def test_a_name_defined_BOTH_ways_is_left_alone(self, tmp_path):
        """`shutdown` is async on three classes here and sync on NiceGUI's app.
        A call site cannot be judged without knowing which, so it is not
        guessed at — the alternative is false positives, and a gate with those
        gets disabled."""
        hits = self._scan(tmp_path,
                          "async def shutdown():\n"
                          "    pass\n", name="a.py")
        hits += self._scan(tmp_path,
                           "def shutdown():\n"
                           "    pass\n"
                           "def build():\n"
                           "    shutdown()\n", name="b.py")

        assert hits == []

    def test_an_attribute_call_is_not_reported(self, tmp_path):
        """`app.shutdown()` — NiceGUI's, synchronous, same name as ours."""
        hits = self._scan(tmp_path,
                          "async def shutdown():\n"
                          "    pass\n"
                          "def build():\n"
                          "    app.shutdown()\n")

        assert hits == []

    def test_a_syntax_error_is_skipped_not_crashed_on(self, tmp_path):
        assert self._scan(tmp_path, "def f(:\n") == []
