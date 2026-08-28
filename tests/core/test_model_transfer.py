"""Packaging and unpacking ML model snapshots between nodes.

This is a one-shot, user-triggered copy of model files from one machine's
DATA_DIR to another's. Both directions carry a security property that the
module's own comments call out, and neither had a test.

  * SENDING is an allowlist, not "everything in DATA_DIR". That directory also
    holds bridge credentials, session files and the trading databases. A glob
    that widened by accident would ship all of it to the other node.

  * RECEIVING rejects path traversal. The bytes arrive over a websocket from
    the peer, so "the sender-side allowlist should never produce one" is not a
    guarantee about what actually turns up. A zip entry named
    ../../bridge_credentials.json must be refused, not written.

No network. Everything runs against tmp_path.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from backend.src.services.cluster.sync import model_transfer as mt


@pytest.fixture
def data_dir(tmp_path):
    """A DATA_DIR that looks like a real one: models plus things that must
    never travel."""
    d = tmp_path / "data"
    d.mkdir()
    # models -- these SHOULD be packaged
    (d / "bo_ml_batch.joblib").write_bytes(b"breakout-model")
    (d / "ml_signal_v2.joblib").write_bytes(b"signal-model")
    (d / "re_ml_online.pkl").write_bytes(b"reversal-model")
    # everything else -- these must NOT be
    (d / "bridge_credentials.json").write_bytes(b'{"password":"hunter2"}')
    (d / "forex_trader_demo.db").write_bytes(b"SQLite format 3\x00")
    (d / "forex_trader.log").write_bytes(b"log contents")
    (d / "dashboard_password.hash").write_bytes(b"deadbeef")
    (d / "re_ml_batch.pkl.bak").write_bytes(b"a backup, not a model")
    return d


class TestPackagingIsAnAllowlist:
    def test_it_packages_the_model_files(self, data_dir):
        _, names = mt.package_models(data_dir)
        assert set(names) == {"bo_ml_batch.joblib", "ml_signal_v2.joblib",
                              "re_ml_online.pkl"}

    @pytest.mark.parametrize("secret", [
        "bridge_credentials.json",
        "forex_trader_demo.db",
        "dashboard_password.hash",
        "forex_trader.log",
    ])
    def test_it_never_packages_anything_else(self, data_dir, secret):
        """The failure that matters. DATA_DIR holds the MT5 credentials, the
        dashboard password hash and the trading database; a widened glob would
        put them on the wire to the other node."""
        zip_bytes, names = mt.package_models(data_dir)
        assert secret not in names
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert secret not in zf.namelist()

    def test_the_bytes_of_a_secret_never_appear_in_the_archive(self, data_dir):
        """Belt and braces: not just absent by name."""
        zip_bytes, _ = mt.package_models(data_dir)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            blob = b"".join(zf.read(n) for n in zf.namelist())
        assert b"hunter2" not in blob
        assert b"SQLite format 3" not in blob

    def test_an_empty_data_dir_produces_a_valid_empty_archive(self, tmp_path):
        """A freshly set-up node has no models yet. It must produce a readable
        zip rather than raising."""
        empty = tmp_path / "empty"; empty.mkdir()
        zip_bytes, names = mt.package_models(empty)
        assert names == []
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            assert zf.namelist() == []


class TestRoundTrip:
    def test_models_survive_package_then_unpack(self, data_dir, tmp_path):
        dest = tmp_path / "dest"; dest.mkdir()
        zip_bytes, names = mt.package_models(data_dir)

        written = mt.unpack_models(zip_bytes, dest)

        assert sorted(written) == sorted(names)
        assert (dest / "re_ml_online.pkl").read_bytes() == b"reversal-model"

    def test_unpack_overwrites_an_existing_model(self, data_dir, tmp_path):
        """The whole point -- seeding a node with a more mature model."""
        dest = tmp_path / "dest"; dest.mkdir()
        (dest / "re_ml_online.pkl").write_bytes(b"old-and-worse")
        zip_bytes, _ = mt.package_models(data_dir)

        mt.unpack_models(zip_bytes, dest)

        assert (dest / "re_ml_online.pkl").read_bytes() == b"reversal-model"


class TestUnpackRejectsTraversal:
    """The archive arrives over a websocket from the peer. What the sender
    SHOULD have produced is not a guarantee about what arrives."""

    def _zip_with(self, entry_name: str, payload: bytes = b"evil") -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(entry_name, payload)
        return buf.getvalue()

    @pytest.mark.parametrize("evil", [
        "../bridge_credentials.json",
        "../../bridge_credentials.json",
        "../../../etc/passwd",
        "subdir/model.pkl",
        "/absolute/model.pkl",
    ])
    def test_it_refuses_to_write_outside_the_target_directory(self, tmp_path, evil):
        dest = tmp_path / "dest"; dest.mkdir()
        sentinel = tmp_path / "bridge_credentials.json"
        sentinel.write_bytes(b"ORIGINAL")

        written = mt.unpack_models(self._zip_with(evil), dest)

        assert written == [], f"accepted a traversal entry: {evil!r}"
        assert sentinel.read_bytes() == b"ORIGINAL", "a file outside dest was overwritten"

    def test_a_good_entry_alongside_a_bad_one_still_lands(self, tmp_path):
        """One poisoned entry must not discard the whole transfer."""
        dest = tmp_path / "dest"; dest.mkdir()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("../evil.pkl", b"evil")
            zf.writestr("re_ml_online.pkl", b"good-model")

        written = mt.unpack_models(buf.getvalue(), dest)

        assert written == ["re_ml_online.pkl"]
        assert (dest / "re_ml_online.pkl").read_bytes() == b"good-model"
        assert not (tmp_path / "evil.pkl").exists()
