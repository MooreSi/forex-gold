"""The running build reports 0.612 "The VPS trades Telegram itself", from one
source, everywhere.

It replaces the 0.611 "Windows install and VPS setup" pin (owner, 2026-09-29)
with the same assertions: the release moved, so the pin moves with it, as it
did from 0.61 to 0.611. Nothing in the app compares versions numerically, so
the string is all that matters.

"Everywhere" is the part with teeth. v0.42's own release notes record that
this went wrong before: Settings > Update and the admin console's per-client
version were reading a separately-maintained file that had drifted out of sync
with the real release, and CHANGELOG.md had sat three releases behind. So this
pins that every reporter derives from RELEASES[0] rather than checking one of
them and assuming the rest agree.
"""
from __future__ import annotations

import pathlib

from backend.src.controllers import remote_controller, system_controller
from backend.src.utils import version_history as vh

REPO = pathlib.Path(__file__).resolve().parents[2]


class TestTheReleaseItself:
    def test_the_head_entry_is_0_612(self):
        assert vh.RELEASES[0][0] == "v0.612"

    def test_it_is_called_the_vps_trades_telegram_itself(self):
        assert vh.RELEASES[0][1] == "The VPS trades Telegram itself"

    def test_it_says_what_changed(self):
        """An empty release in the About screen is worse than no entry."""
        changes = vh.RELEASES[0][4]

        assert len(changes) >= 3
        assert all(isinstance(c, str) and len(c) > 40 for c in changes)

    def test_the_previous_release_is_still_there(self):
        """A version bump must not eat the history it is a history of."""
        assert vh.RELEASES[1][0] == "v0.611"
        assert vh.RELEASES[2][0] == "v0.61"
        assert vh.RELEASES[3][0] == "v0.6"
        assert len(vh.RELEASES) >= 14


class TestEveryReporterAgrees:
    def test_the_derived_version(self):
        assert vh.__version__ == "0.612"

    def test_the_settings_and_about_reporter(self):
        assert system_controller.app_version() == "0.612"

    def test_the_reporter_the_admin_console_reads(self):
        """This is the one that goes out over the HELLO handshake. It reported
        a stale version once already."""
        assert remote_controller.app_version() == "0.612"

    def test_the_VERSION_file_fallback(self):
        """For callers that cannot import the package. It is derived, not
        hand-maintained -- importing version_history rewrites it."""
        assert (REPO / "VERSION").read_text(encoding="utf-8").strip() == "0.612"

    def test_they_all_agree(self):
        """The actual property. Any single one of these being right while
        another drifts is exactly the v0.42 bug."""
        assert len({
            vh.__version__,
            system_controller.app_version(),
            remote_controller.app_version(),
            (REPO / "VERSION").read_text(encoding="utf-8").strip(),
        }) == 1


class TestTheChangelog:
    def test_it_has_a_0_612_entry(self):
        head = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")[:400]

        assert "v0.612" in head
        assert "The VPS trades Telegram itself" in head
