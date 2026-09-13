"""A service that declares its surface must have someone using it.

Service modules that declare `__all__` are making an explicit statement of what
they are for. Counting the whole layer, 155 names are exported and 9 are
referenced by nothing.

Three of those nine are Bounce's `panel_data`, already gated by
`test_panel_reads_have_a_panel.py` and excluded here so one deletion is not
reported twice. That leaves **97 of 103 in scope alive**, and six that are not
— ordinary drift, a function written for a caller that changed its mind, or an
async variant added beside a sync one and never adopted:

  * `app_config.set_async`, `pnl.hourly_grid_async` — the async halves of
    sync/async pairs. Their sync twins are in use; nothing ever took the
    off-the-loop route.
  * `log_events.since_last_start`, `log_events.MAX_LINES`
  * `template_simulator.TemplateResult`, `template_support.UNSUPPORTED_WHEN_ON`

None of them is a bug. The point of the gate is the next one: a service export
with no caller is how ~3,000 lines of code nothing called accumulated before
the audit, one reasonable-looking addition at a time.

Shrink-only. The set may lose entries and must never gain one.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from tests.refactor._source_scan import REPO, references_to

# Gated separately, by test_panel_reads_have_a_panel.py.
_ALREADY_GATED = ("panel_data.py",)


def _modules() -> list[str]:
    out = []
    for p in sorted((REPO / "backend/src/services").rglob("*.py")):
        rel = p.relative_to(REPO).as_posix()
        if p.name == "__init__.py" or p.name in _ALREADY_GATED:
            continue
        if "__all__" in p.read_text(encoding="utf-8"):
            out.append(rel)
    return out


_MODULES = _modules()

KNOWN_DEAD = {
    ("app_config", "set_async"),
    ("pnl", "hourly_grid_async"),
    ("log_events", "since_last_start"),
    ("log_events", "MAX_LINES"),
    ("template_simulator", "TemplateResult"),
    ("template_support", "UNSUPPORTED_WHEN_ON"),
}


def _dead() -> set[tuple[str, str]]:
    out = set()
    for rel in _MODULES:
        try:
            mod = importlib.import_module(rel[:-3].replace("/", "."))
        except Exception:                       # noqa: BLE001
            continue
        for name in getattr(mod, "__all__", []):
            if name.startswith("_"):
                continue
            if not references_to(name, exclude=(rel,)):
                out.add((Path(rel).stem, name))
    return out


class TestEveryDeclaredExportIsUsed:
    def test_no_new_unused_service_exports(self):
        unexpected = _dead() - KNOWN_DEAD

        assert not unexpected, (
            f"service exports nothing references: {sorted(unexpected)} — a "
            "module that declares its surface is saying what it is for. Wire "
            "it up or delete it; do not add it to KNOWN_DEAD."
        )

    def test_the_known_dead_set_has_no_slack(self):
        assert _dead() == KNOWN_DEAD

    def test_the_layer_is_overwhelmingly_alive(self):
        """The ratio that makes this a gate rather than a wish. If it ever
        drifts, this file is measuring the wrong thing and should be re-argued
        rather than baselined."""
        exported = 0
        for rel in _MODULES:
            try:
                mod = importlib.import_module(rel[:-3].replace("/", "."))
            except Exception:                   # noqa: BLE001
                continue
            exported += sum(1 for n in getattr(mod, "__all__", [])
                            if not n.startswith("_"))

        assert exported > 90
        assert len(_dead()) <= 10


class TestTheScopeIsWhatItClaims:
    def test_panel_data_modules_are_left_to_their_own_gate(self):
        """Otherwise the three reads orphaned by one panel deletion are
        reported twice, and fixing them there would fail here."""
        assert not any(m.endswith("panel_data.py") for m in _MODULES)

    def test_there_are_modules_to_check(self):
        assert len(_MODULES) > 15


class TestTheScannerCanSee:
    def test_a_used_export_is_seen_as_used(self):
        assert references_to("suggest_lot_size", exclude=())

    def test_a_name_nothing_mentions_is_seen_as_dead(self):
        assert not references_to("service_export_that_never_existed", exclude=())
