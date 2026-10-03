"""An EA NEWER than this app's source is not a stale .ex5.

Reported 2026-10-03. The chart ran EA v1.09 (edited directly in MetaTrader's
Experts folder and never copied back), the repo shipped v1.08, and the badge
said "the compiled .ex5 is stale ... run tools/deploy_ea.sh". Following that
advice would have overwritten the v1.09 source with v1.08 and dropped the
template-isolation fix. The check was `!=`, so it could not tell behind from
ahead.
"""
from __future__ import annotations

from backend.src.services.broker import ea_bridge
from backend.src.services.broker.ea_bridge import _version


class _Bridge:
    ea_version_ok = False
    ea_source_drift_s = 0.0

    def __init__(self, running):
        self.ea_version = running


def _status(monkeypatch, running, shipped):
    monkeypatch.setattr(ea_bridge, "get_instance", lambda: _Bridge(running))
    monkeypatch.setattr(_version, "_expected_ea_version", lambda: shipped)
    return ea_bridge.ea_build_status()


def test_an_ea_ahead_of_the_app_is_still_flagged(monkeypatch):
    stale, _ = _status(monkeypatch, "1.09", "1.08")
    assert stale is True


def test_an_ea_ahead_of_the_app_is_not_told_to_run_deploy(monkeypatch):
    _, detail = _status(monkeypatch, "1.09", "1.08")
    assert "Do not run tools/deploy_ea.sh" in detail
    assert "run tools/deploy_ea.sh, then" not in detail
    assert "newer" in detail and "1.09" in detail and "1.08" in detail
    assert "stale" not in detail.lower()


def test_an_older_ea_still_gets_the_deploy_advice(monkeypatch):
    _, detail = _status(monkeypatch, "1.07", "1.08")
    assert "deploy_ea" in detail


def test_versions_compare_numerically_not_as_text(monkeypatch):
    _, detail = _status(monkeypatch, "1.10", "1.9")
    assert "newer" in detail


def test_an_unparseable_version_falls_back_to_the_stale_advice(monkeypatch):
    _, detail = _status(monkeypatch, "weird", "1.08")
    assert "deploy_ea" in detail
