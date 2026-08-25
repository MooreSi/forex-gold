"""The plain-English "what does this update change?" blurb behind the header's
Update Available popup (core_app_update.changes_digest / summarise_changes).

What matters here:
  * the digest describes the PENDING commits and nothing else -- a summary
    that includes commits the user already has, or omits ones they don't, is
    worse than no summary at all;
  * summarising is decorative. No missing API key, provider outage, timeout or
    junk reply may ever raise out of summarise_changes(), because the same
    popup's Update Now button has to keep working without an LLM.

Uses a real throwaway git repo (git plumbing is exactly what's under test, so
faking it would test nothing) and a faked AI provider, so no network call and
no live repo is touched.
"""
import contextlib
import os
import subprocess
from unittest import mock

import pytest

from backend.src.services.ai import provider
from backend.src.services.positions import core_app_update as cau


def _git(repo, *args):
    env = {
        **os.environ,
        "HOME": str(repo),            # ignore the developer's own git config
        "GIT_CONFIG_GLOBAL": str(repo / ".gitconfig-none"),
        "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e.invalid",
        "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e.invalid",
    }
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True,
        capture_output=True, text=True, env=env,
    )


def _rev(repo, ref="HEAD"):
    return subprocess.run(
        ["git", "rev-parse", ref], cwd=str(repo), check=True,
        capture_output=True, text=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A checkout with three commits, with core_app_update pointed at it."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")

    (r / "a.txt").write_text("one\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "already installed commit")
    base = _rev(r)

    (r / "a.txt").write_text("one\ntwo\n")
    _git(r, "add", "a.txt")
    _git(r, "commit", "-q", "-m", "Stop the spread widget flickering",
         "-m", "It repainted on every tick.")

    (r / "b.txt").write_text("new file\n")
    _git(r, "add", "b.txt")
    _git(r, "commit", "-q", "-m", "Add a pause-until-news setting")
    head = _rev(r)

    monkeypatch.setattr(cau, "_REPO_ROOT", r)
    return {"path": r, "base": base, "head": head}


# ── changes_digest ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_digest_covers_pending_commits_only(repo):
    digest = await cau.changes_digest(repo["base"], repo["head"])

    assert "Stop the spread widget flickering" in digest
    assert "Add a pause-until-news setting" in digest
    assert "It repainted on every tick." in digest, "commit bodies carry the why"
    assert "already installed commit" not in digest, (
        "the commit the user already has must not be described as incoming"
    )
    assert "b.txt" in digest, "the diffstat should say which files move"


@pytest.mark.asyncio
async def test_digest_empty_when_nothing_pending(repo):
    assert await cau.changes_digest(repo["head"], repo["head"]) == ""


@pytest.mark.asyncio
async def test_digest_empty_for_missing_or_unknown_shas(repo):
    assert await cau.changes_digest("", repo["head"]) == ""
    assert await cau.changes_digest(repo["base"], "") == ""
    assert await cau.changes_digest(repo["base"], "0" * 40) == ""


@pytest.mark.asyncio
async def test_digest_honours_max_commits(repo):
    digest = await cau.changes_digest(repo["base"], repo["head"], max_commits=1)

    assert "Add a pause-until-news setting" in digest, "newest commit kept"
    assert "Stop the spread widget flickering" not in digest


@pytest.mark.asyncio
async def test_digest_truncated_to_prompt_budget(repo, monkeypatch):
    monkeypatch.setattr(cau, "_DIGEST_MAX_CHARS", 80)

    digest = await cau.changes_digest(repo["base"], repo["head"])

    assert digest.endswith("[... truncated ...]")
    assert len(digest) <= 80 + len("\n[... truncated ...]")


# ── summarise_changes ─────────────────────────────────────────────────────────

@contextlib.contextmanager
def _provider(complete, configured=True):
    """Swap the real LLM call out for `complete` so no request leaves the box."""
    with mock.patch.object(ai_provider, "complete", complete), \
         mock.patch.object(ai_provider, "is_configured", lambda cfg: configured):
        yield


@pytest.mark.asyncio
async def test_summary_bullets_are_stripped_of_markers(repo):
    async def complete(cfg, system, prompt, max_tokens, timeout):
        return ("- The spread readout no longer flickers.\n"
                "\n"
                "* You can pause trading around news.\n"
                "• Internal tidy-up.")

    with _provider(complete):
        bullets, error = await cau.summarise_changes(
            repo["base"], repo["head"], cfg={"anthropic_api_key": "k"},
        )

    assert error == ""
    assert bullets == [
        "The spread readout no longer flickers.",
        "You can pause trading around news.",
        "Internal tidy-up.",
    ]


@pytest.mark.asyncio
async def test_summary_prompt_contains_the_pending_commits(repo):
    seen = {}

    async def complete(cfg, system, prompt, max_tokens, timeout):
        seen["system"] = system
        seen["prompt"] = prompt
        return "- ok"

    with _provider(complete):
        await cau.summarise_changes(
            repo["base"], repo["head"], cfg={"anthropic_api_key": "k"},
        )

    assert "Add a pause-until-news setting" in seen["prompt"]
    assert "already installed commit" not in seen["prompt"]


@pytest.mark.asyncio
async def test_no_provider_configured_is_reported_not_raised(repo):
    async def complete(*a, **kw):
        raise AssertionError("must not call an unconfigured provider")

    with _provider(complete, configured=False):
        bullets, error = await cau.summarise_changes(repo["base"], repo["head"], cfg={})

    assert bullets == []
    assert "no AI provider configured" in error


@pytest.mark.asyncio
async def test_provider_failure_is_reported_not_raised(repo):
    async def complete(cfg, system, prompt, max_tokens, timeout):
        raise TimeoutError("provider timed out")

    with _provider(complete):
        bullets, error = await cau.summarise_changes(
            repo["base"], repo["head"], cfg={"anthropic_api_key": "k"},
        )

    assert bullets == []
    assert "TimeoutError" in error and "provider timed out" in error


@pytest.mark.asyncio
async def test_blank_reply_is_reported_not_returned_as_a_bullet(repo):
    async def complete(cfg, system, prompt, max_tokens, timeout):
        return "  \n-\n  \n"

    with _provider(complete):
        bullets, error = await cau.summarise_changes(
            repo["base"], repo["head"], cfg={"anthropic_api_key": "k"},
        )

    assert bullets == []
    assert error == "empty summary from AI provider"


@pytest.mark.asyncio
async def test_no_pending_commits_skips_the_provider(repo):
    async def complete(*a, **kw):
        raise AssertionError("nothing to summarise -- provider must not be called")

    with _provider(complete):
        bullets, error = await cau.summarise_changes(
            repo["head"], repo["head"], cfg={"anthropic_api_key": "k"},
        )

    assert bullets == []
    assert error == "no commit details available"
