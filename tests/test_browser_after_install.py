"""The first start after an install opens the dashboard, even on a VPS.

Every Remote-role (VPS) start skips the browser on purpose: an unattended tab
on a small VPS was implicated in event-loop stalls, and restarts, updates and
the keep-alive task all launch the app with nobody looking. But the start the
installer's "Launch FOREX Trader now" box triggers has a person watching it,
and on 2026-09-25 that person got a console and no dashboard, and had to type
the address in by hand.

The installer leaves a marker; the first start that finds it opens the browser
once and removes it. Nothing else about the VPS rule changes.
"""
from __future__ import annotations

import run


def test_the_first_start_after_an_install_opens_the_browser_on_a_vps(tmp_path):
    marker = tmp_path / "open_browser_once"
    marker.write_text("", encoding="utf-8")

    assert run._should_open_browser(no_browser=False, is_vps=True, marker=marker) is True


def test_the_marker_is_used_up_by_that_start(tmp_path):
    marker = tmp_path / "open_browser_once"
    marker.write_text("", encoding="utf-8")

    run._should_open_browser(no_browser=False, is_vps=True, marker=marker)

    assert not marker.exists()
    assert run._should_open_browser(no_browser=False, is_vps=True, marker=marker) is False


def test_a_vps_start_with_no_marker_still_skips_the_browser(tmp_path):
    assert run._should_open_browser(
        no_browser=False, is_vps=True, marker=tmp_path / "absent") is False


def test_the_marker_opens_it_even_through_no_browser(tmp_path):
    """The installer's launch goes through the .bat, whose restarts pass
    --no-browser. If the first run crashes (a missing DLL, 2026-09-25) the
    relaunch that finally comes up is the one the person is waiting on."""
    marker = tmp_path / "open_browser_once"
    marker.write_text("", encoding="utf-8")

    assert run._should_open_browser(no_browser=True, is_vps=True, marker=marker) is True


def test_a_local_machine_behaves_as_before(tmp_path):
    absent = tmp_path / "absent"
    assert run._should_open_browser(no_browser=False, is_vps=False, marker=absent) is True
    assert run._should_open_browser(no_browser=True, is_vps=False, marker=absent) is False
