"""Normalisation must erase the mechanical differences of extraction and
nothing else. If it erases too much it hides truncations; too little and the
diffs are unreadable and go unread.
"""
from __future__ import annotations

import ast

from tools.refactor_audit.ast_normalise import (
    decorator_names,
    find_function,
    normalised_source,
    statement_shape,
)


def fn(src: str):
    return find_function(ast.parse(src), "f")


METHOD = """
def f(self, trade_id):
    '''Docstring that should not survive.'''
    result = self._bridge.close_position(trade_id)
    return self._record_close(result)
"""

EXTRACTED = """
def f(bridge, trade_id):
    result = bridge.close_position(trade_id)
    return record_close(result)
"""


def test_a_faithful_extraction_normalises_to_the_same_source():
    assert normalised_source(fn(METHOD)) == normalised_source(fn(EXTRACTED))


def test_docstrings_are_stripped():
    assert "Docstring" not in normalised_source(fn(METHOD))


def test_signature_differences_are_erased():
    a = "def f(self, a, b=1, *args, **kw): return a"
    b = "def f(bridge, a, tp_cache, b=1): return a"
    assert normalised_source(fn(a)) == normalised_source(fn(b))


def test_ctx_receiver_is_transparent():
    a = "def f(ctx): return ctx.tp_cache"
    b = "def f(tp_cache): return tp_cache"
    assert normalised_source(fn(a)) == normalised_source(fn(b))


def test_a_dropped_assignment_is_not_erased():
    """The core_run_tp_ladder failure: `current_sl = new_sl` silently lost."""
    full = "def f(sl):\n    current_sl = sl\n    return current_sl\n"
    truncated = "def f(sl):\n    return sl\n"
    assert normalised_source(fn(full)) != normalised_source(fn(truncated))
    assert len(statement_shape(fn(full))) > len(statement_shape(fn(truncated)))


def test_a_dropped_trailing_call_is_not_erased():
    """The core_handle_orb_fixed failure: a trailing log.info lost."""
    full = "def f():\n    do_work()\n    log.info('done')\n"
    truncated = "def f():\n    do_work()\n"
    assert len(statement_shape(fn(full))) > len(statement_shape(fn(truncated)))


def test_statement_shape_ignores_names_but_counts_structure():
    a = "def f():\n    if x:\n        return 1\n"
    b = "def f():\n    if wholly_different_name:\n        return 2\n"
    assert statement_shape(fn(a)) == statement_shape(fn(b))


def test_statement_shape_sees_nested_statements():
    shallow = "def f():\n    return 1\n"
    nested = "def f():\n    for i in x:\n        if i:\n            return 1\n"
    assert len(statement_shape(fn(nested))) > len(statement_shape(fn(shallow)))


def test_decorators_are_reported_separately():
    """The @contextmanager the database-split script dropped."""
    assert decorator_names(fn("@contextmanager\ndef f(): pass")) == ["contextmanager"]
    assert decorator_names(fn("def f(): pass")) == []


def test_dunder_names_are_left_alone():
    src = normalised_source(fn("def f(x): return x.__class__"))
    assert "__class__" in src
