"""`ui.element(...).text = x` renders nothing, and says nothing about it.

Found 2026-09-16 from the screen, not from the code: the Performance
Analytics and ML tables on both engine panels drew a header row with a
bottom border and no words in it. The headers had been written, were
correct, and had never once been visible.

    ui.element("th").classes("text-left px-1 py-0.5").text = h   # silent

`nicegui.Element` has **no `text` property**. It keeps `self._text` and
forwards it to the client only when set through the constructor or a
subclass that manages it (`ui.label`, `ui.button`). Assigning `.text` on a
plain `ui.element` just binds a new Python attribute that nothing ever
reads. No error, no warning, no text.

`ui.label(...).text = x` IS valid and is used correctly in ~40 places, so
this cannot be a blanket ban on the attribute name. The rule is narrower:
the assignment target must not be a bare `ui.element(...)` chain.

The fix everywhere is what the BODY cells of those same tables already did:

    with ui.element("th").classes(...):
        ui.label(h)
"""
from __future__ import annotations

import ast
import pathlib

import pytest
from nicegui import ui
from nicegui.client import Client

REPO = pathlib.Path(__file__).resolve().parents[2]


def _base_call(node: ast.AST) -> ast.AST | None:
    """Unwrap a chain like `ui.element("th").classes(...).props(...)` down to
    the call that started it. Every chained helper returns the element, so the
    assignment target's type is decided by the base call alone."""
    while isinstance(node, ast.Call):
        f = node.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Call):
            node = f.value
            continue
        return node
    return None


def _is_ui_element_call(node: ast.AST) -> bool:
    base = _base_call(node)
    if not isinstance(base, ast.Call):
        return False
    f = base.func
    return (isinstance(f, ast.Attribute) and f.attr == "element"
            and isinstance(f.value, ast.Name)
            and f.value.id in ("ui", "_ui", "nicegui"))


def _offenders() -> list[str]:
    found = []
    for path in sorted((REPO / "frontend").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(REPO).as_posix()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if (isinstance(target, ast.Attribute) and target.attr == "text"
                        and _is_ui_element_call(target.value)):
                    found.append(f"{rel}:{target.lineno}")
    return found


def test_nicegui_element_really_has_no_text_attribute():
    """The premise, asserted rather than assumed, and asserted the only way
    that actually discriminates.

    Two earlier versions of this test were wrong. The first built
    `ui.element("div")` with no client context, which left NiceGUI's slot
    stack broken for every later test in the run -- eight unrelated failures
    in `test_risk_card_has_no_out_of_hours` and `test_schedule_banner_refreshes`,
    each passing in isolation. The second checked the CLASS for a `text`
    property to avoid building anything, which proves nothing: `ui.label`
    has no class-level `text` property either. It is an INSTANCE attribute
    set in `TextElement.__init__`. Only a real instance can tell them apart,
    so the element is built inside a `Client`, the same way the rest of this
    package's render tests do it.
    """
    with Client(lambda: None, request=None):
        assert not hasattr(ui.element("div"), "text")


def test_that_check_can_see_the_attribute_when_there_is_one():
    """Negative control. `ui.label` DOES carry `text`, which is why
    `lbl.text = x` works and is used correctly all over this package."""
    with Client(lambda: None, request=None):
        assert hasattr(ui.label("a"), "text")


def test_no_page_assigns_text_to_a_bare_element():
    assert _offenders() == []


def test_the_scanner_finds_a_planted_one():
    """Negative control, planted in a real file and found by the same walk."""
    planted = REPO / "frontend" / "components" / "_planted_text_assign.py"
    planted.write_text(
        "from nicegui import ui\n\n\n"
        "def _x():\n"
        '    ui.element("th").classes("a").text = "Header"\n',
        encoding="utf-8")
    try:
        found = _offenders()
    finally:
        planted.unlink()
    assert "frontend/components/_planted_text_assign.py:5" in found


def test_a_label_text_assignment_is_not_flagged():
    """`ui.label(...).text = x` is correct and common. A rule that banned it
    would be rewritten to pass within a day."""
    planted = REPO / "frontend" / "components" / "_planted_label_assign.py"
    planted.write_text(
        "from nicegui import ui\n\n\n"
        "def _x():\n"
        '    lbl = ui.label("a")\n'
        '    lbl.text = "b"\n'
        '    ui.label("c").text = "d"\n',
        encoding="utf-8")
    try:
        found = _offenders()
    finally:
        planted.unlink()
    assert not [f for f in found if "_planted_label_assign" in f]
