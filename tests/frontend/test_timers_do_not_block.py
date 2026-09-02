"""A `ui.timer` callback that reads the database must not do it on the event loop.

NiceGUI runs a sync timer callback inline on the event loop. If that callback
reaches a controller, and the controller reaches the database, then every tick
stops the whole dashboard — every other page, every websocket, every other
timer — for the duration of a SQLite read. At a 2-second period, on a machine
that is also running the trading loops, that is a stall the user experiences as
the UI "sticking".

The fix is not to stop using timers. NiceGUI awaits an `async def` callback
properly, and the services already expose async twins that offload via
`to_db_thread`. So the rule is narrow:

    a timer callback that reaches a controller must be `async def`

Not every timer — 23 of the 36 were already async, and most of the sync ones
only touch labels they already hold. This gate is about the ones that cross
into the backend.

Structural, and it names the offenders rather than counting them: a baseline
number would let a new blocking timer in as long as an old one was fixed.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"

# How a page reaches the backend. Controllers are imported under these names
# throughout frontend/ (the import contract forbids anything deeper).
_CONTROLLER_HINTS = ("_ctl", "_controller")

# Sites this gate deliberately does not fail, each with the reason. An entry
# here is a decision on the record, not a suppression: a timer that reaches a
# controller is still a stall, and this list says why that stall is accepted.
_EXEMPT = {
    "frontend/pages/history/_heatmap.py:_daily_8am_check":
        "Body is a datetime compare. It reaches the controller only inside a "
        "two-minute window once a day (08:00-08:02) and then hands off to an "
        "async task; the two reads it makes before that are not worth a "
        "restructure of the analysis renderer.",
}


def _controller_aliases(tree: ast.AST) -> set:
    """Every local name bound to a controller module, INCLUDING aliases.

    The first version of this gate matched owner names against `_ctl` /
    `_controller` and reported zero offenders while three sites were live:
    `news_controller as nc` and `engines_controller as pro_model` bind names
    that contain neither hint. A guardrail that reads the import statements
    cannot be fooled by what the import is called.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if "backend.src.controllers" in node.module:
                names.update(a.asname or a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if "backend.src.controllers" in a.name:
                    names.add(a.asname or a.name.split(".")[-1])
    return names


def _reaches_a_controller(fn: ast.AST, aliases: set | None = None,
                          fns: dict | None = None, _depth: int = 0) -> str:
    """The first controller call inside `fn`, or "" if it makes none.

    Follows calls to local helper functions as well, to a bounded depth: the
    heatmap's timer body is a datetime compare that calls a helper which reads
    the database, and a one-level check called that clean.
    """
    aliases = aliases or set()
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        owner = getattr(node.func, "value", None)
        name = getattr(owner, "id", "") or getattr(owner, "attr", "")
        if name and (name in aliases or any(h in name for h in _CONTROLLER_HINTS)):
            return f"{name}.{getattr(node.func, 'attr', '?')}"
        # A local helper the callback calls is part of the callback's cost.
        callee = getattr(node.func, "id", "")
        if (_depth < 2 and fns and callee in fns
                and fns[callee] is not fn
                and not isinstance(fns[callee], ast.AsyncFunctionDef)):
            deeper = _reaches_a_controller(fns[callee], aliases, fns, _depth + 1)
            if deeper:
                return f"{callee}() -> {deeper}"
    return ""


def _blocking_timer_callbacks(root: pathlib.Path | None = None) -> list[str]:
    root = root or FRONTEND
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        src = path.read_text(encoding="utf-8")
        if "ui.timer" not in src:
            continue
        tree = ast.parse(src)
        fns = {n.name: n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "timer"
                    and len(node.args) >= 2):
                continue
            cb = node.args[1]
            fn = fns.get(getattr(cb, "id", None) or getattr(cb, "attr", None) or "")
            if fn is None or isinstance(fn, ast.AsyncFunctionDef):
                continue
            call = _reaches_a_controller(fn, _controller_aliases(tree), fns)
            if call:
                rel = str(path.relative_to(REPO if root is FRONTEND else root)).replace("\\", "/")
                if f"{rel}:{fn.name}" in _EXEMPT:
                    continue
                offenders.append(f"{rel}:{node.lineno} {fn.name}() -> {call}")
    return offenders


def test_no_sync_timer_callback_reaches_a_controller():
    offenders = _blocking_timer_callbacks()

    assert offenders == [], (
        "these timer callbacks are synchronous and reach the backend, so every "
        "tick blocks the event loop for the whole dashboard:\n  "
        + "\n  ".join(offenders)
        + "\n\nMake the callback `async def` and await the service's async twin "
          "(the ones that end in `_async` offload via to_db_thread)."
    )


class TestTheDetectorWorks:
    """Negative controls. A detector that found nothing would satisfy the gate
    above permanently."""

    def _offenders(self, tmp_path, source: str) -> list:
        f = tmp_path / "page.py"
        f.write_text(source, encoding="utf-8")
        tree = ast.parse(source)
        fns = {n.name: n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        out = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "timer" and len(node.args) >= 2):
                fn = fns.get(getattr(node.args[1], "id", "") or "")
                if fn is not None and not isinstance(fn, ast.AsyncFunctionDef):
                    if _reaches_a_controller(fn, _controller_aliases(tree), fns):
                        out.append(fn.name)
        return out

    def test_it_catches_a_sync_callback_that_reads_the_backend(self, tmp_path):
        src = ("def build():\n"
               "    def _tick():\n"
               "        lbl.text = risk_ctl.get_risk_settings()['x']\n"
               "    ui.timer(2.0, _tick)\n")

        assert self._offenders(tmp_path, src) == ["_tick"]

    def test_it_catches_an_ALIASED_controller_import(self, tmp_path):
        """The blind spot that let three live sites through. `nc` contains
        neither `_ctl` nor `_controller`, so the name-substring version of this
        detector reported zero offenders while the stalls were real."""
        src = ("from backend.src.controllers import news_controller as nc\n"
               "def build():\n"
               "    def _tick():\n"
               "        lbl.text = nc.get_events()\n"
               "    ui.timer(2.0, _tick)\n")

        assert self._offenders(tmp_path, src) == ["_tick"]

    def test_it_catches_a_controller_reached_through_a_local_helper(self, tmp_path):
        """The second blind spot. The callback body is a clock compare; the
        database read is one call further down."""
        src = ("from backend.src.controllers import trading_controller as t\n"
               "def build():\n"
               "    def _label():\n"
               "        return t.get_risk_settings()\n"
               "    def _tick():\n"
               "        lbl.text = _label()\n"
               "    ui.timer(2.0, _tick)\n")

        assert self._offenders(tmp_path, src) == ["_tick"]

    def test_a_plain_local_helper_is_not_flagged(self, tmp_path):
        """The recursion must not turn every timer into an offender."""
        src = ("def build():\n"
               "    def _fmt(v):\n"
               "        return str(v)\n"
               "    def _tick():\n"
               "        lbl.text = _fmt(1)\n"
               "    ui.timer(2.0, _tick)\n")

        assert self._offenders(tmp_path, src) == []

    def test_the_gate_itself_can_still_fail(self, tmp_path):
        """The exemption list runs inside the scanner, so a bug there disables
        the whole gate silently -- and with zero real offenders, nothing else
        would notice. Plant one and require it to be reported."""
        (tmp_path / "page.py").write_text(
            "from backend.src.controllers import risk_controller as rc\n"
            "def build():\n"
            "    def _tick():\n"
            "        lbl.text = rc.get_risk_settings()\n"
            "    ui.timer(2.0, _tick)\n", encoding="utf-8")

        assert _blocking_timer_callbacks(tmp_path)

    def test_mutually_recursive_helpers_do_not_hang_the_detector(self, tmp_path):
        """The depth bound is why this terminates. Without it the scan follows
        A -> B -> A until Python's recursion limit, and the gate dies with a
        RecursionError instead of a verdict."""
        (tmp_path / "page.py").write_text(
            "def build():\n"
            "    def _a():\n"
            "        return _b()\n"
            "    def _b():\n"
            "        return _a()\n"
            "    def _tick():\n"
            "        return _a()\n"
            "    ui.timer(2.0, _tick)\n", encoding="utf-8")

        assert _blocking_timer_callbacks(tmp_path) == []

    def test_every_exemption_still_points_at_real_code(self):
        """A stale exemption is a hole nobody can see. If the site is renamed
        or fixed, the entry must go."""
        for key in _EXEMPT:
            rel, fn_name = key.rsplit(":", 1)
            path = REPO / rel
            assert path.exists(), f"exempted file is gone: {rel}"
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = {n.name for n in ast.walk(tree)
                     if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            assert fn_name in names, f"exempted callback is gone: {key}"

    def test_an_async_callback_is_fine(self, tmp_path):
        src = ("def build():\n"
               "    async def _tick():\n"
               "        lbl.text = (await risk_ctl.get_risk_settings_async())['x']\n"
               "    ui.timer(2.0, _tick)\n")

        assert self._offenders(tmp_path, src) == []

    def test_a_sync_callback_that_only_touches_the_ui_is_fine(self, tmp_path):
        """The point of naming controllers rather than banning sync callbacks:
        most of them just update a label they already hold."""
        src = ("def build():\n"
               "    def _tick():\n"
               "        lbl.text = 'still here'\n"
               "    ui.timer(2.0, _tick)\n")

        assert self._offenders(tmp_path, src) == []


class TestTheAsyncTwinActuallyOffloads:
    """The gate above only checks the callback is `async def`. That is worth
    nothing if the thing it awaits still blocks -- so this checks the twin the
    header badge now uses really does hop off the loop."""

    @pytest.mark.asyncio
    async def test_circuit_breaker_state_async_runs_the_read_off_the_loop(
            self, monkeypatch):
        import backend.src.services.risk.settings as settings

        offloaded: list = []

        async def _fake_to_db_thread(fn, *a, **kw):
            offloaded.append(fn)
            return {"is_active": False}
        monkeypatch.setattr(settings, "to_db_thread", _fake_to_db_thread)

        result = await settings.circuit_breaker_state_async()

        assert offloaded, "the read was NOT offloaded -- it ran on the event loop"
        assert result == {"is_active": False}

    @pytest.mark.asyncio
    async def test_it_returns_the_same_thing_as_the_sync_version(self,
                                                                 monkeypatch):
        """Control: offloading must not change the answer, or every screen
        reading it starts lying."""
        import backend.src.services.risk.settings as settings

        sentinel = {"enabled": True, "is_active": True, "remaining_secs": 42}
        monkeypatch.setattr(settings._breaker, "get_circuit_breaker_state",
                            lambda: sentinel)

        assert settings.circuit_breaker_state() == sentinel
        assert await settings.circuit_breaker_state_async() == sentinel
