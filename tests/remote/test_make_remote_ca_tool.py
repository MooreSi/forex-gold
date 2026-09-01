"""The operator-facing command for the admin CA.

This is run by hand, rarely, under pressure -- setting up a new admin server or
recovering one. Its refusals matter more than its happy path: the one that
protects the owner is `init` refusing to overwrite an authority, because doing
that invalidates every certificate it ever signed and every copy already
shipped inside an app build.
"""
from __future__ import annotations

import pytest

from tools import make_remote_ca


class TestInit:
    def test_it_creates_an_authority(self, tmp_path, capsys):
        assert make_remote_ca.main(["init", "--dir", str(tmp_path / "ca")]) == 0

        assert (tmp_path / "ca" / "ca_cert.pem").exists()
        assert (tmp_path / "ca" / "ca_key.pem").exists()

    def test_it_says_which_file_must_stay_offline(self, tmp_path, capsys):
        """The whole security of the design rests on the operator knowing
        this. A tool that prints two filenames without saying which is the
        secret invites it onto the VPS."""
        make_remote_ca.main(["init", "--dir", str(tmp_path / "ca")])
        out = capsys.readouterr().out

        assert "ca_key.pem" in out and "OFFLINE" in out

    def test_it_refuses_to_overwrite(self, tmp_path, capsys):
        make_remote_ca.main(["init", "--dir", str(tmp_path / "ca")])

        assert make_remote_ca.main(["init", "--dir", str(tmp_path / "ca")]) == 1
        assert "refused" in capsys.readouterr().err

    def test_the_existing_authority_is_untouched_by_a_refusal(self, tmp_path):
        make_remote_ca.main(["init", "--dir", str(tmp_path / "ca")])
        before = (tmp_path / "ca" / "ca_cert.pem").read_bytes()

        make_remote_ca.main(["init", "--dir", str(tmp_path / "ca")])

        assert (tmp_path / "ca" / "ca_cert.pem").read_bytes() == before


class TestIssue:
    def test_it_signs_a_certificate(self, tmp_path):
        make_remote_ca.main(["init", "--dir", str(tmp_path / "ca")])

        rc = make_remote_ca.main([
            "issue", "--dir", str(tmp_path / "ca"),
            "--address", "203.0.113.7", "--out", str(tmp_path / "srv"),
        ])

        assert rc == 0
        assert (tmp_path / "srv" / "server_cert.pem").exists()

    def test_several_addresses_can_be_given(self, tmp_path):
        make_remote_ca.main(["init", "--dir", str(tmp_path / "ca")])

        rc = make_remote_ca.main([
            "issue", "--dir", str(tmp_path / "ca"),
            "--address", "203.0.113.7", "--address", "192.168.1.50",
            "--out", str(tmp_path / "srv"),
        ])

        assert rc == 0

    def test_it_refuses_without_an_authority(self, tmp_path, capsys):
        rc = make_remote_ca.main([
            "issue", "--dir", str(tmp_path / "nothing"),
            "--address", "203.0.113.7", "--out", str(tmp_path / "srv"),
        ])

        assert rc == 1
        assert "run `init` first" in capsys.readouterr().err

    def test_it_does_not_leave_a_half_written_certificate(self, tmp_path):
        """Negative control on the refusal above: a tool that errors after
        writing is worse than one that errors before."""
        make_remote_ca.main([
            "issue", "--dir", str(tmp_path / "nothing"),
            "--address", "203.0.113.7", "--out", str(tmp_path / "srv"),
        ])

        assert not (tmp_path / "srv" / "server_cert.pem").exists()
