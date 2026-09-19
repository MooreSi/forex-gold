"""The version number belongs to the checkout, never to the shared data dir.

Two checkouts of this app run against one USER_DATA_DIR by design -- one
config.yaml, one forex_trader_<env>.db, one set of trade history -- so that
the owner can switch between the original app and the React one without
losing anything. The version number is the deliberate exception. Bumping one
app must not change what the other reports, because the version is how every
consumer of it answers "which code is this?":

- Settings > Update's "Installed Version"
- the header's update badge, via `check_for_update`
- the admin console's per-client version column
- the remote client's HELLO and its ~60s status heartbeat

A version that came from shared state would make all four describe whichever
app was launched last, and the console would show one machine flipping
between two versions while nothing on it changed.

These tests are the guard on that. They are deliberately about WHERE the
number comes from, not what it is.
"""
from pathlib import Path

import backend.src.config as config
from backend.src.controllers import system_controller
from backend.src.utils import version_history
from backend.src.utils.os_utils import repo_root


class TestTheVersionComesFromTheCheckout:
    def test_it_is_the_newest_release_in_this_repo_s_history(self):
        assert version_history.__version__ == version_history.RELEASES[0][0].lstrip("v")

    def test_the_version_file_it_syncs_is_the_repo_root_one(self):
        """`VERSION` is the fallback for callers that cannot import the
        package -- the remote client and server read it. It has to be the
        checkout's own copy: read from the shared data directory it would be
        whichever app wrote it last."""
        written = repo_root() / "VERSION"
        assert written.exists()
        assert written.read_text(encoding="utf-8").strip() == version_history.__version__

    def test_the_controller_agrees_with_it(self):
        """`app_version()` is what the API, the node endpoints and the admin
        console all report. A second derivation here is a second answer."""
        assert system_controller.app_version() == version_history.__version__


class TestTheSharedDataDirectoryHasNoSayInIt:
    def test_deriving_it_leaves_a_decoy_in_the_data_dir_untouched(self, tmp_path, monkeypatch):
        """The test that would catch the mistake.

        A decoy rather than an empty directory, because an empty one proves
        nothing: `_derive_version()` swallows the read error on a file that is
        not there and then skips the write, so a version wired to the shared
        directory would pass an emptiness check without doing anything. Give
        it a file with the wrong number in it and the two behaviours separate
        -- the checkout's own VERSION is rewritten, this one is not read and
        not touched.
        """
        monkeypatch.setattr(config, "USER_DATA_DIR", tmp_path)
        monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
        (tmp_path / "data").mkdir()
        for decoy in (tmp_path / "VERSION", tmp_path / "data" / "VERSION"):
            decoy.write_text("99.9\n", encoding="utf-8")

        assert version_history._derive_version() == version_history.__version__

        for decoy in (tmp_path / "VERSION", tmp_path / "data" / "VERSION"):
            assert decoy.read_text(encoding="utf-8") == "99.9\n", (
                f"{decoy} was rewritten -- the version number is being synced "
                f"into the directory both checkouts share"
            )
        assert (repo_root() / "VERSION").read_text(encoding="utf-8").strip() == \
            version_history.__version__

    def test_no_version_file_sits_in_the_real_data_dir(self):
        """Belt and braces against the file simply being put there one day.
        The real directory, not a tmp one, because that is the one both apps
        share."""
        shared = Path(config.USER_DATA_DIR)
        if not shared.exists():
            return
        assert not (shared / "VERSION").exists()
        assert not (shared / "data" / "VERSION").exists()
