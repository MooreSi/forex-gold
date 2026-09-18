"""Helpers for behaviours whose UI was deleted before its replacement existed.

The big-bang React replace on 2026-09-18 deleted eight NiceGUI tabs ahead of
their React equivalents. Several tests across the suite asserted things about
those tabs — a switch is reachable, a toggle is gone, a handler captures its
widgets. Their subject no longer exists.

Deleting those tests outright would be the cheap answer and the wrong one: the
behaviour they protect is still wanted, and a deleted test protects nothing the
day the tab comes back. Skipping them is explicitly forbidden (golden rule 4).

So they become **conditional guards**. While the tab is marked not-ported, the
test asserts exactly that — and the moment somebody clears the `notPorted` flag
in `frontend/src/components/shell/tabs.ts` without restoring the behaviour, the
test goes red and names what is missing. The requirement survives the gap
instead of being quietly dropped into it.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TABS_FILE = REPO / "frontend" / "src" / "components" / "shell" / "tabs.ts"
WEB_SRC = REPO / "frontend" / "src"


def tab_is_ported(tab_id: str) -> bool:
    """True once `tabs.ts` says this tab has a real React panel.

    Reads the source rather than a build artefact: the flag is the thing a
    developer changes, and it changes in the same commit as the panel.
    """
    source = TABS_FILE.read_text(encoding="utf-8")
    match = re.search(
        r'id:\s*"' + re.escape(tab_id) + r'".*?notPorted:\s*(null|\{)',
        source,
        re.DOTALL,
    )
    if not match:
        raise AssertionError(
            f"no tab with id {tab_id!r} in {TABS_FILE.relative_to(REPO)} — the "
            "tab was renamed or removed, so this guard is pointing at nothing"
        )
    return match.group(1) == "null"


def web_sources() -> str:
    """Every TypeScript source in the dashboard, concatenated.

    A substring search over this is a weak check and is meant to be: it proves
    a name is referenced somewhere in the UI, which is enough to notice that a
    ported tab dropped a requirement. The component tests under
    `frontend/src/**/__tests__/` are what prove the behaviour.
    """
    parts = [p.read_text(encoding="utf-8")
             for p in sorted(WEB_SRC.rglob("*.ts*"))
             if "__tests__" not in p.parts]
    if not parts:
        raise AssertionError(
            f"no TypeScript found under {WEB_SRC.relative_to(REPO)} — a search "
            "over nothing finds nothing and would pass every negative check"
        )
    return "\n".join(parts)
