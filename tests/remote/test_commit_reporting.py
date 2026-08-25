"""A client must report which commit it is running, or say why it can't.

The admin console's Remote Clients card showed "no commit reported" for a Mac
that had been installed and registered the same day (v0.42, online), with a
tooltip blaming code "from before commit-SHA reporting existed". Both halves
were wrong-headed:

  * get_local_commit_sha() returned "" for three unrelated situations -- no
    .git at all, no usable `git` binary, and a git command that failed -- and
    swallowed all of them into the same empty string, so nothing downstream
    could tell them apart. The install scripts copy files and never clone, so
    a freshly installed machine legitimately has no .git until its first
    update: the normal state of a new client rendered as an unexplained blank.
  * git itself is not guaranteed on a client. "FOREX Start.command" only
    installs it when Homebrew is present, and on macOS an Xcode CLT stub or a
    "dubious ownership" refusal makes every `git` call exit non-zero inside a
    perfectly good checkout.

These tests use real git repositories on disk, and drive the real reporting
path down to the JSON the client puts on the wire.
"""
import shutil
import subprocess

import pytest

from backend.src.services.positions import core_app_update as cau
from backend.src.services.cluster.remote import client as rc
from backend.src.services.cluster.remote import server as rs


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=str(cwd), check=True,
                   capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    """A real single-commit git checkout."""
    root = tmp_path / "checkout"
    root.mkdir()
    _git("init", "-q", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.com", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "run.py").write_text("x = 1\n", encoding="utf-8")
    _git("add", "run.py", cwd=root)
    _git("commit", "-qm", "first", cwd=root)
    return root


@pytest.fixture
def head_sha(repo):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True).stdout.strip()


def _no_git_binary(monkeypatch):
    """Every `git` invocation fails the way a machine without git fails."""
    def _boom(*a, **kw):
        raise FileNotFoundError("git")
    monkeypatch.setattr(cau.subprocess, "run", _boom)
    monkeypatch.setattr(cau.shutil, "which", lambda _: None)


def test_reports_head_from_a_real_checkout(monkeypatch, repo, head_sha):
    monkeypatch.setattr(cau, "_REPO_ROOT", repo)
    sha, note = cau.get_commit_report()
    assert note == ""
    assert head_sha.startswith(sha)
    assert cau.get_commit_report(short=False) == (head_sha, "")


def test_reports_head_without_a_usable_git_binary(monkeypatch, repo, head_sha):
    """The whole point of the fallback: the checkout is fine, git is not."""
    monkeypatch.setattr(cau, "_REPO_ROOT", repo)
    _no_git_binary(monkeypatch)
    assert cau.get_commit_report(short=False) == (head_sha, "")
    assert cau.get_commit_report() == (head_sha[:7], "")


def test_reports_head_from_packed_refs_without_git(monkeypatch, repo, head_sha):
    """`git gc`/`pack-refs` removes the loose ref file the fallback reads
    first; a packed repo must still resolve."""
    _git("pack-refs", "--all", cwd=repo)
    assert not (repo / ".git" / "refs" / "heads" / "main").exists()
    monkeypatch.setattr(cau, "_REPO_ROOT", repo)
    _no_git_binary(monkeypatch)
    assert cau.get_commit_report(short=False) == (head_sha, "")


def test_reports_detached_head_without_git(monkeypatch, repo, head_sha):
    _git("checkout", "-q", "--detach", "HEAD", cwd=repo)
    monkeypatch.setattr(cau, "_REPO_ROOT", repo)
    _no_git_binary(monkeypatch)
    assert cau.get_commit_report(short=False) == (head_sha, "")


def test_worktree_resolves_through_commondir(monkeypatch, tmp_path, repo):
    """A linked worktree's .git is a file, and its refs live in the main
    repo's dir -- the fallback has to follow both hops."""
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", str(wt), "-b", "side", cwd=repo)
    wt_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(wt),
                            capture_output=True, text=True).stdout.strip()
    assert (wt / ".git").is_file()
    monkeypatch.setattr(cau, "_REPO_ROOT", wt)
    _no_git_binary(monkeypatch)
    assert cau.get_commit_report(short=False) == (wt_sha, "")


def test_fresh_copy_install_says_no_checkout(monkeypatch, tmp_path, repo):
    """What the .bat/.command install scripts actually produce: the files,
    without .git. Not a fault -- but it must be distinguishable from one."""
    copied = tmp_path / "copied"
    shutil.copytree(repo, copied, ignore=shutil.ignore_patterns(".git"))
    monkeypatch.setattr(cau, "_REPO_ROOT", copied)
    assert cau.get_commit_report() == ("", cau.COMMIT_NO_CHECKOUT)


def test_broken_checkout_is_reported_as_unreadable(monkeypatch, repo):
    monkeypatch.setattr(cau, "_REPO_ROOT", repo)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/gone\n", encoding="utf-8")
    _no_git_binary(monkeypatch)
    assert cau.get_commit_report() == ("", cau.COMMIT_GIT_UNREADABLE)


def test_get_local_commit_sha_still_returns_a_bare_sha(monkeypatch, repo, head_sha):
    """Existing callers (admin console's Latest Commit card) are unchanged."""
    monkeypatch.setattr(cau, "_REPO_ROOT", repo)
    assert cau.get_local_commit_sha(short=False) == head_sha


# ── What actually goes on the wire ───────────────────────────────────────────

def test_hello_and_status_carry_the_commit_and_its_reason(monkeypatch, tmp_path, repo):
    monkeypatch.setattr(cau, "_REPO_ROOT", repo)
    monkeypatch.setattr(rc, "get_or_create_token", lambda: "tok")
    from backend.src.services.cluster.remote import ip_check
    monkeypatch.setattr(ip_check, "get_machine_uuid", lambda: "uuid")

    sha = cau.get_local_commit_sha()
    assert rc._build_hello()["commit_sha"] == sha
    assert rc._build_hello()["commit_note"] == ""
    assert rc._build_status()["commit_sha"] == sha

    copied = tmp_path / "copied"
    shutil.copytree(repo, copied, ignore=shutil.ignore_patterns(".git"))
    monkeypatch.setattr(cau, "_REPO_ROOT", copied)
    hello = rc._build_hello()
    assert hello["commit_sha"] == ""
    assert hello["commit_note"] == cau.COMMIT_NO_CHECKOUT
    assert rc._build_status()["commit_note"] == cau.COMMIT_NO_CHECKOUT


# ── The console's view of an offline client ──────────────────────────────────

def test_offline_client_keeps_its_last_known_build(monkeypatch):
    """version/commit_sha only ever lived on the in-memory _connected entry,
    which disconnect discards -- so every offline client showed "unknown" and
    no commit, however recently it had reported one."""
    monkeypatch.setattr(rs, "_connected", {})
    monkeypatch.setattr(rs, "_allowed_tokens", {"tok": {"name": "Mac"}})

    rs._remember_build("tok", "0.42", "86e920c", "")
    (client,) = rs.get_all_clients()
    assert client["online"] is False
    assert client["version"] == "0.42"
    assert client["commit_sha"] == "86e920c"


def test_a_blank_sha_never_erases_a_known_one(monkeypatch):
    """A client that momentarily can't read its checkout shouldn't wipe the
    last good answer -- but it should still explain itself."""
    monkeypatch.setattr(rs, "_connected", {})
    monkeypatch.setattr(rs, "_allowed_tokens", {"tok": {"name": "Mac"}})

    rs._remember_build("tok", "0.42", "86e920c", "")
    rs._remember_build("tok", "0.42", "", cau.COMMIT_GIT_UNREADABLE)
    (client,) = rs.get_all_clients()
    assert client["commit_sha"] == "86e920c"
    assert client["commit_note"] == cau.COMMIT_GIT_UNREADABLE


def test_no_checkout_reason_survives_to_the_console(monkeypatch):
    monkeypatch.setattr(rs, "_connected", {})
    monkeypatch.setattr(rs, "_allowed_tokens", {"tok": {"name": "Mac"}})

    rs._remember_build("tok", "0.42", "", cau.COMMIT_NO_CHECKOUT)
    (client,) = rs.get_all_clients()
    assert client["commit_sha"] == ""
    assert client["commit_note"] == cau.COMMIT_NO_CHECKOUT
