"""Which button Settings > Update offers, for each answer from the checker.

The card only ever rendered an Update button when an update was *available*,
which a machine with no `.git` can never be: `check_for_update()` stops at the
missing checkout. So a freshly installed machine sat on "Check failed / not a
git checkout" with nothing to press, even though `apply_update()` has known how
to bootstrap a checkout since 2026-09-03. Reported live 2026-09-06 from a Mac
on the LAN that could not update itself at all.

`_update_action()` is the decision on its own, so it can be checked without
rendering NiceGUI: the closure that draws the row does nothing but read it.
"""
from __future__ import annotations

from frontend.pages.update_panel import _update_action


def test_a_pending_update_offers_the_update_button():
    assert _update_action({"available": True, "commits": [{}]}) == "update"


def test_a_missing_checkout_offers_the_bootstrap_button():
    assert _update_action({"available": False, "bootstrap": True}) == "bootstrap"


def test_being_up_to_date_offers_neither():
    assert _update_action({"available": False, "commits": []}) == ""


def test_a_check_that_failed_for_any_other_reason_offers_neither():
    """A fetch that could not reach GitHub is not a reason to force-checkout
    the working tree -- Check for Updates is the retry."""
    assert _update_action(
        {"available": False, "error": "fatal: unable to access remote"}) == ""


def test_no_git_binary_offers_neither():
    """`bootstrap` is False precisely because the bootstrap cannot run; a
    button that always fails is worse than the message saying to install git.
    """
    assert _update_action(
        {"available": False, "bootstrap": False,
         "error": "git is not installed on this machine..."}) == ""
