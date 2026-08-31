"""Silent-except ratchet for the frontend (stage2 phase4/030).

`except Exception: pass` in a live-money dashboard swallows refresh and
fetch failures, leaving stale-but-plausible numbers on screen with no log
and no indicator. The count REGRESSED 31 → 44 between the 2026-08-08 and
2026-08-11 reviews; this gate stops the climb and ratchets it down.

Nothing here can reach a broker.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"

# Shrinking baseline — lower it as swallows are converted to logged
# handlers; never raise it.
# 44 -> 40 -> 38 (2026-08-25 merge) -> 0 (2026-08-31).
#
# The remaining 38 were converted in one pass: every one is now
# `except Exception as e:` followed by a debug log naming the page and what
# was being refreshed. None was deleted or narrowed away, and none changed
# behaviour — a swallowed failure is still swallowed, it just says so now.
#
# At zero this stops being a ratchet and becomes a rule: the frontend does not
# silently swallow exceptions. Which is the point — a live-money dashboard
# showing stale-but-plausible numbers with no log and no indicator is the
# failure mode this gate was created for.
SILENT_EXCEPT_PASS_MAX = 0


def _silent_except_passes(root: Path) -> list[str]:
    """Every `except:`/`except Exception:` whose body is exactly `pass`."""
    offenders: list[str] = []
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            broad = node.type is None or (
                isinstance(node.type, ast.Name) and node.type.id == "Exception"
            )
            body_is_pass = len(node.body) == 1 and isinstance(node.body[0], ast.Pass)
            if broad and body_is_pass:
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    return sorted(offenders)


def test_no_new_silent_except_pass_under_frontend():
    offenders = _silent_except_passes(FRONTEND)
    assert len(offenders) <= SILENT_EXCEPT_PASS_MAX, (
        f"{len(offenders)} silent `except Exception: pass` in frontend/ "
        f"(baseline {SILENT_EXCEPT_PASS_MAX}) — log the failure or narrow the "
        f"except instead of swallowing it. New ones:\n  " + "\n  ".join(offenders)
    )


def test_baseline_is_not_slack():
    """The baseline sits AT the real count — slack is room to regress."""
    assert len(_silent_except_passes(FRONTEND)) == SILENT_EXCEPT_PASS_MAX


def test_detector_can_see_a_swallow(tmp_path):
    """Negative control: both the bare and the Exception form are caught,
    and a logged handler is NOT flagged."""
    p = tmp_path / "sample.py"
    p.write_text(
        "try:\n    x = 1\nexcept Exception:\n    pass\n"
        "try:\n    y = 2\nexcept:\n    pass\n"
        "try:\n    z = 3\nexcept Exception as e:\n    print(e)\n",
        encoding="utf-8",
    )
    offenders = _silent_except_passes(tmp_path)
    assert len(offenders) == 2
