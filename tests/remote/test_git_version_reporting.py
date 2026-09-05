"""A client must say which `git` it has, not only whether it has a checkout.

The admin console could tell a machine with no `.git` (COMMIT_NO_CHECKOUT --
the normal state of an installer-copied install) from one whose checkout is
unreadable, but it could not say anything at all about the `git` binary: a
machine with no git and a machine that has simply never self-updated looked
identical. Since the update path is `git fetch`/`git pull`, "is there a git
here" is the first thing you want to know when a client will not update.

The version travels on the heartbeat (MSG_STATUS) and on the HELLO, so the
console has it on connect and does not wait a minute for the first beat.
"""
import json
import subprocess
import types

import pytest

from backend.src.services.positions import core_app_update as cau
from backend.src.services.cluster.remote import client as rc
from backend.src.services.cluster.remote import server as rs
from backend.src.services.cluster.remote.protocol import MSG_HELLO, MSG_STATUS


@pytest.fixture(autouse=True)
def unprobed(monkeypatch):
    """get_git_version() probes once per process and remembers the answer;
    every test here needs that memory empty or it reads the previous test's."""
    monkeypatch.setattr(cau, "_GIT_VERSION", None)


def _git_says(monkeypatch, stdout: str, returncode: int = 0):
    calls = []

    def _fake_run(args, **kwargs):
        calls.append(list(args))
        return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
    monkeypatch.setattr(cau.subprocess, "run", _fake_run)
    return calls


# ── Reading the version ──────────────────────────────────────────────────────

def test_reports_the_version_of_the_real_git_on_this_machine():
    """No fakes: whatever `git --version` prints here must be parsed into a
    bare version number."""
    installed = subprocess.run(["git", "--version"], capture_output=True, text=True)
    assert installed.returncode == 0, "this machine has no git; the test proves nothing"

    version = cau.get_git_version()

    assert version, "a working git reported no version"
    assert version in installed.stdout
    assert version[0].isdigit(), f"{version!r} is not a version number"


def test_the_apple_git_suffix_is_not_part_of_the_version(monkeypatch):
    _git_says(monkeypatch, "git version 2.39.5 (Apple Git-154)\n")

    assert cau.get_git_version() == "2.39.5"


def test_no_git_binary_reports_no_version(monkeypatch):
    def _boom(*_a, **_kw):
        raise FileNotFoundError("git")
    monkeypatch.setattr(cau.subprocess, "run", _boom)

    assert cau.get_git_version() == ""


def test_a_stub_that_exits_non_zero_reports_no_version(monkeypatch):
    """macOS without the Command Line Tools: /usr/bin/git exists and fails."""
    _git_says(monkeypatch, "", returncode=1)

    assert cau.get_git_version() == ""


def test_output_that_is_not_a_git_version_line_reports_no_version(monkeypatch):
    _git_says(monkeypatch, "xcrun: error: invalid active developer path\n")

    assert cau.get_git_version() == ""


def test_git_is_asked_only_once_per_process(monkeypatch):
    """The heartbeat runs every ~60s. On a Mac without the Command Line Tools
    each `git` invocation can raise the "install developer tools" dialog, so
    the probe must happen once and be remembered -- not once a minute."""
    calls = _git_says(monkeypatch, "git version 2.39.5\n")

    assert cau.get_git_version() == "2.39.5"
    assert cau.get_git_version() == "2.39.5"

    assert len(calls) == 1, f"git was run {len(calls)} times"


def test_no_version_is_remembered_too(monkeypatch):
    """The failure answer has to stick for the same reason the good one does:
    a missing git is exactly the machine whose dialog must not reappear."""
    calls = _git_says(monkeypatch, "", returncode=1)

    assert cau.get_git_version() == ""
    assert cau.get_git_version() == ""

    assert len(calls) == 1


# ── What goes on the wire ────────────────────────────────────────────────────

@pytest.fixture
def token(monkeypatch):
    monkeypatch.setattr(rc, "get_or_create_token", lambda: "tok")
    from backend.src.services.cluster.remote import ip_check
    monkeypatch.setattr(ip_check, "get_machine_uuid", lambda: "uuid")


def test_the_heartbeat_carries_the_git_version(monkeypatch, token):
    _git_says(monkeypatch, "git version 2.39.5\n")

    assert rc._build_status()["git_version"] == "2.39.5"


def test_the_hello_carries_the_git_version(monkeypatch, token):
    """So the console shows it on connect rather than a minute later."""
    _git_says(monkeypatch, "git version 2.39.5\n")

    assert rc._build_hello()["git_version"] == "2.39.5"


def test_a_machine_with_no_checkout_still_reports_its_git(monkeypatch, tmp_path, token):
    """The case that prompted this: "no git checkout" is about `.git`, and
    said nothing about whether git was installed. Both facts now travel."""
    monkeypatch.setattr(cau, "_REPO_ROOT", tmp_path)
    _git_says(monkeypatch, "git version 2.39.5\n")

    status = rc._build_status()

    assert status["commit_note"] == cau.COMMIT_NO_CHECKOUT
    assert status["git_version"] == "2.39.5"


def test_a_machine_with_no_git_reports_an_empty_version(monkeypatch, token):
    _git_says(monkeypatch, "", returncode=1)

    assert rc._build_status()["git_version"] == ""


def test_the_client_survives_a_git_probe_that_raises(monkeypatch, token):
    """A heartbeat is not allowed to fail because git misbehaved."""
    def _boom():
        raise RuntimeError("probe exploded")
    monkeypatch.setattr(cau, "get_git_version", _boom)

    assert rc._build_status()["git_version"] == ""


# ── What the console ends up seeing ──────────────────────────────────────────


class _Ws:
    """The scripted fake from test_connection_auth.py: serves frames, records
    what came back, and snapshots the console's view of the client before each
    frame -- by the time _handler returns the session entry is gone."""

    def __init__(self, frames, seen):
        self._frames = list(frames)
        self.remote_address = ("203.0.113.9", 51234)
        self.sent: list = []
        self.seen = seen

    async def recv(self):
        if not self._frames:
            raise TimeoutError("no more frames")
        return json.dumps(self._frames.pop(0))

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def close(self):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        self._snapshot()
        if not self._frames:
            raise StopAsyncIteration
        return json.dumps(self._frames.pop(0))

    def _snapshot(self):
        entry = rs._connected.get("GOOD")
        if entry:
            self.seen.append(dict(entry["info"]))


@pytest.fixture
def known_client(monkeypatch, tmp_path):
    """One approved token, no disk writes anywhere real, no admin fan-out."""
    d = tmp_path / "remote"
    d.mkdir()
    monkeypatch.setattr(rs, "_REMOTE_DIR", d)
    monkeypatch.setattr(rs, "_TOKENS_FILE", d / "allowed_tokens.json")
    monkeypatch.setattr(rs, "_connected", {})
    monkeypatch.setattr(rs, "_admin_clients", {})
    monkeypatch.setattr(rs, "_allowed_tokens", {"GOOD": {"name": "Simon's Mac"}})

    async def _noop(*_a, **_kw):
        return None
    monkeypatch.setattr(rs, "_push_clients_to_all_admins", _noop)


async def _client_info(frames) -> list[dict]:
    """Drive one session; return what the console would have rendered after
    each frame."""
    seen: list[dict] = []
    await rs._handler(_Ws(frames, seen))
    return seen


@pytest.mark.asyncio
class TestTheServerKeepsTheGitVersion:

    async def test_the_hello_version_reaches_the_client_list(self, known_client):
        seen = await _client_info(
            [{"type": MSG_HELLO, "token": "GOOD", "git_version": "2.39.5"}])

        assert seen, "the session was never registered"
        assert seen[-1]["git_version"] == "2.39.5"

    async def test_a_heartbeat_updates_it(self, known_client):
        """A client that installs git mid-session (or restarts into one) says
        so on its next beat."""
        seen = await _client_info([
            {"type": MSG_HELLO, "token": "GOOD", "git_version": ""},
            {"type": MSG_STATUS, "git_version": "2.39.5"},
        ])

        assert seen[0]["git_version"] == ""
        assert seen[-1]["git_version"] == "2.39.5"

    async def test_a_heartbeat_without_the_field_keeps_the_known_version(
            self, known_client):
        """An older client sends no git_version at all. Its HELLO answer must
        survive, not be blanked once a minute."""
        seen = await _client_info([
            {"type": MSG_HELLO, "token": "GOOD", "git_version": "2.39.5"},
            {"type": MSG_STATUS},
        ])

        assert seen[-1]["git_version"] == "2.39.5"
