"""A name a module uses but never defines is a bug that ships green.

The failure this gate exists for: **a split leaves a function's dependency
behind.** Code moves to a new module, callers are updated, the suite passes --
and the moved function still refers to a constant that stayed in the original
file. Nothing fails until that exact line runs in production, which for a
rarely-taken branch can be a long time.

This repo has had it four times:

    docs/todo/bugs/010   undefined `ap` in test_panel
    docs/todo/bugs/011   undefined `_SIGNAL_GEN_SYSTEM` in ai_trade_analysis
    2026-08-30           the cluster splits -- 15 names across 4 files,
                         including `_read_changelog`, which every successful
                         client connection calls

All four shipped through `python -m tools.checks all` at 8/8, because nothing
looked for this and the coverage in the affected files was 17-27%.

The scanner is `tools/refactor_audit/undefined_names.py` -- stdlib only, on
purpose: pyflakes would do this and more, but it is not a declared dependency
of this project and a scanner does not get to add one.
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

from tools.refactor_audit import undefined_names as un

REPO = pathlib.Path(__file__).resolve().parents[2]
ROOTS = ["backend", "frontend", "tools", "run.py", "mt5_bridge.py"]


def test_no_module_uses_a_name_it_never_defines():
    """The gate. Zero, and it stays zero."""
    findings = un.scan([REPO / r for r in ROOTS])

    assert findings == [], (
        "undefined name(s) found -- a split almost certainly left a "
        "dependency behind:\n  " + "\n  ".join(
            str(f).replace(str(REPO) + "/", "") for f in findings)
    )


class TestTheScannerActuallyWorks:
    """Negative controls. A scanner that found nothing would satisfy the gate
    above forever, which is precisely the shape of the vacuous guardrail this
    repo's rules were written after."""

    def _findings_for(self, tmp_path, source: str):
        f = tmp_path / "sample.py"
        f.write_text(source, encoding="utf-8")
        return un.check_file(f, "sample.py")

    def test_it_catches_the_real_bug_shape(self, tmp_path):
        """Exactly what the 2026-08-30 split produced: a moved function still
        referring to a constant that stayed behind."""
        hits = self._findings_for(tmp_path, "def read():\n    return _LEFT_BEHIND\n")

        assert [h.name for h in hits] == ["_LEFT_BEHIND"]

    def test_it_catches_it_in_an_annotation(self, tmp_path):
        """Under `from __future__ import annotations` this never evaluates, so
        it does not crash -- but the import was still left behind."""
        hits = self._findings_for(
            tmp_path,
            "from __future__ import annotations\n"
            "def f(x) -> Optional[int]:\n    return x\n",
        )

        assert [h.name for h in hits] == ["Optional"]

    @pytest.mark.parametrize("source", [
        "import os\ndef f():\n    return os.getcwd()\n",             # module import
        "X = 1\ndef f():\n    return X\n",                            # module const
        "def f(a, b=2, *rest, **kw):\n    return a, b, rest, kw\n",   # params
        "def f():\n    y = 1\n    return y\n",                        # local
        "def f(items):\n    return [i for i in items if i]\n",        # comprehension
        "def f(items):\n    return {k: v for k, v in items}\n",       # dict comp
        "def f():\n    return len([])\n",                             # builtin
        "def f(xs):\n    return sorted(xs, key=lambda x: x.n)\n",     # lambda param
        "def outer():\n    z = 1\n    def inner():\n        return z\n    return inner\n",
        "G = 0\ndef f():\n    global G\n    G = 1\n",                 # global
        "try:\n    import ujson as j\nexcept ImportError:\n    import json as j\n"
        "def f():\n    return j\n",                                   # conditional import
        "def f(p):\n    with open(p) as fh:\n        return fh.read()\n",
        "def f():\n    for i in range(3):\n        pass\n    return i\n",
        "def f(x):\n    if (y := x + 1):\n        return y\n    return 0\n",  # walrus
        "class C:\n    A = 1\n    def m(self):\n        return C.A\n",
        "def f():\n    try:\n        pass\n    except ValueError as e:\n        return e\n",
    ])
    def test_it_does_not_cry_wolf(self, tmp_path, source):
        """Every one of these is legitimate. A scanner with false positives
        gets its gate disabled, which is worse than not having one."""
        assert self._findings_for(tmp_path, source) == []

    def test_a_class_attribute_is_NOT_visible_inside_its_own_methods(
            self, tmp_path):
        """Python's actual scoping rule, and the easiest one to get wrong in
        the other direction -- a scanner that leaked class names into methods
        would miss real bugs."""
        hits = self._findings_for(
            tmp_path, "class C:\n    A = 1\n    def m(self):\n        return A\n")

        assert [h.name for h in hits] == ["A"]

    def test_a_star_import_makes_a_module_unanalysable_and_is_skipped(
            self, tmp_path):
        """Guessing would produce false positives on every name."""
        hits = self._findings_for(
            tmp_path, "from os.path import *\ndef f():\n    return join('a', 'b')\n")

        assert hits == []

    def test_a_syntax_error_is_skipped_not_crashed_on(self, tmp_path):
        assert self._findings_for(tmp_path, "def f(:\n") == []


@pytest.mark.skipif(
    subprocess.run([sys.executable, "-c", "import pyflakes"],
                   capture_output=True).returncode != 0,
    reason="pyflakes is not a declared dependency; this check runs where it happens to be installed",
)
def test_the_scanner_agrees_with_pyflakes_on_this_tree():
    """Equivalence check, for the machines that have pyflakes.

    This is how the scanner was validated when written: on 2026-08-31 both
    reported the same 15 names, byte for byte. It is skipped rather than
    required, because requiring it would make pyflakes a dependency by the
    back door -- the gate above stands on its own.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "pyflakes", *ROOTS],
        capture_output=True, text=True, cwd=REPO,
    )
    theirs = sorted(
        line.split(":")[0] + ":" + line.split(":")[1] + ":" + line.split("undefined name")[1].strip()
        for line in proc.stdout.splitlines() if "undefined name" in line
    )
    prefix = str(REPO) + "/"
    mine = sorted(
        f"{f.path.replace(prefix, '')}:{f.line}:{f.name!r}"
        for f in un.scan([REPO / r for r in ROOTS])
    )

    assert mine == theirs, (
        f"the scanner and pyflakes disagree.\n"
        f"  only pyflakes: {sorted(set(theirs) - set(mine))}\n"
        f"  only ours:     {sorted(set(mine) - set(theirs))}"
    )
