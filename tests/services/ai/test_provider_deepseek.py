"""The DeepSeek completion path, driven against a stubbed transport.

These lines used to be covered by accident. Two balance-math tests in
`tests/test_signal/test_engine_characterization.py` reached this function
through the engine's close path and made **live, billed calls to
api.deepseek.com** on every local run, with whatever API key the developer had
configured. CI has no key, so it never executed the body — which is the entire
reason `services/ai` sat below its coverage floor on Windows and not on macOS.

The network guard in tests/conftest.py stopped that. These tests replace the
coverage deliberately: same lines, no key, no wire, identical on every machine.

The transport is stubbed rather than the function mocked, so the request that
would have gone out is inspectable — which is how `thinking: disabled` below
is pinned, and that one is not cosmetic.
"""
from __future__ import annotations

import json

import httpx
import pytest

from backend.src.services.ai import provider


def _client(handler):
    """An httpx.AsyncClient whose transport answers in-process."""
    return httpx.MockTransport(handler)


@pytest.fixture
def transport(monkeypatch):
    """Capture the outgoing request and answer it locally."""
    captured: dict = {}

    def _install(response):
        def _handler(request):
            captured["request"] = request
            captured["body"] = json.loads(request.content.decode())
            return response

        real = httpx.AsyncClient.__init__

        def _init(self, *a, **kw):
            kw["transport"] = _client(_handler)
            real(self, *a, **kw)

        monkeypatch.setattr(httpx.AsyncClient, "__init__", _init)
        return captured

    return _install


def _ok(content="  hello  ", finish_reason="stop"):
    return httpx.Response(200, json={
        "choices": [{"message": {"content": content},
                     "finish_reason": finish_reason}]})


CFG = {"deepseek_api_key": "sk-test", "deepseek_model": "deepseek-v4-flash"}


@pytest.mark.asyncio
class TestASuccessfulCompletion:
    async def test_the_reply_text_comes_back_stripped(self, transport):
        transport(_ok("  the answer  "))

        out = await provider._complete_deepseek(CFG, "sys", "prompt", 100, 5)

        assert out == "the answer"

    async def test_it_posts_to_the_completions_endpoint(self, transport):
        cap = transport(_ok())

        await provider._complete_deepseek(CFG, "sys", "prompt", 100, 5)

        assert str(cap["request"].url).endswith("/chat/completions")

    async def test_the_api_key_is_sent_as_a_bearer_token(self, transport):
        cap = transport(_ok())

        await provider._complete_deepseek(CFG, "sys", "prompt", 100, 5)

        assert cap["request"].headers["Authorization"] == "Bearer sk-test"

    async def test_thinking_is_disabled(self, transport):
        """Not cosmetic. Both models default to thinking mode, which spends the
        max_tokens budget on hidden reasoning before writing any answer —
        measured live at max_tokens=50: 100% consumed, empty content. Every
        call site here budgets tokens assuming the whole budget is the answer.
        """
        cap = transport(_ok())

        await provider._complete_deepseek(CFG, "sys", "prompt", 100, 5)

        assert cap["body"]["thinking"] == {"type": "disabled"}

    async def test_a_system_prompt_is_sent_as_its_own_message(self, transport):
        cap = transport(_ok())

        await provider._complete_deepseek(CFG, "sys", "prompt", 100, 5)

        assert cap["body"]["messages"][0] == {"role": "system", "content": "sys"}
        assert cap["body"]["messages"][1] == {"role": "user", "content": "prompt"}

    async def test_an_empty_system_prompt_is_omitted_entirely(self, transport):
        """Callers with no separate system prompt pass "". Sending an empty
        system message is not the same as sending none."""
        cap = transport(_ok())

        await provider._complete_deepseek(CFG, "", "prompt", 100, 5)

        assert [m["role"] for m in cap["body"]["messages"]] == ["user"]

    async def test_the_token_budget_and_model_are_passed(self, transport):
        cap = transport(_ok())

        await provider._complete_deepseek(CFG, "s", "p", 1234, 5)

        assert cap["body"]["max_tokens"] == 1234
        assert cap["body"]["model"] == "deepseek-v4-flash"


@pytest.mark.asyncio
class TestWhenItGoesWrong:
    async def test_a_missing_key_raises_before_any_request(self, transport):
        cap = transport(_ok())

        with pytest.raises(RuntimeError, match="not configured"):
            await provider._complete_deepseek({}, "s", "p", 10, 5)

        assert "request" not in cap

    async def test_an_http_error_raises(self, transport):
        transport(httpx.Response(401, json={"error": "bad key"}))

        with pytest.raises(httpx.HTTPStatusError):
            await provider._complete_deepseek(CFG, "s", "p", 10, 5)

    async def test_a_truncated_reply_raises_rather_than_returning_half(self,
                                                                       transport):
        """The caller parses this as JSON. Half an object is worse than an
        error, because it fails somewhere further away."""
        transport(_ok('{"partial": ', finish_reason="length"))

        with pytest.raises(provider.TruncatedResponseError):
            await provider._complete_deepseek(CFG, "s", "p", 10, 5)


@pytest.mark.asyncio
class TestRouting:
    async def test_deepseek_is_selected_by_the_provider_setting(self, monkeypatch):
        seen: list = []

        async def _fake(cfg, system, prompt, max_tokens, timeout):
            seen.append("deepseek")
            return "x"

        monkeypatch.setattr(provider, "_complete_deepseek", _fake)
        monkeypatch.setattr(provider, "_is_debug", lambda: False)

        await provider.complete({**CFG, "ai_provider": "deepseek"}, "s", "p", 10)

        assert seen == ["deepseek"]

    async def test_claude_is_the_default(self, monkeypatch):
        seen: list = []

        async def _fake(cfg, system, prompt, max_tokens, timeout):
            seen.append("claude")
            return "x"

        monkeypatch.setattr(provider, "_complete_claude", _fake)
        monkeypatch.setattr(provider, "_is_debug", lambda: False)

        await provider.complete({}, "s", "p", 10)

        assert seen == ["claude"]


class TestIsConfigured:
    def test_deepseek_needs_only_its_key(self):
        assert provider.is_configured(
            {"ai_provider": "deepseek", "deepseek_api_key": "k"}) is True

    def test_deepseek_without_a_key_is_not_configured(self):
        assert provider.is_configured({"ai_provider": "deepseek"}) is False


class TestTheImageSniffer:
    """Telegram photos are usually JPEG and not guaranteed to be. Telethon's
    download_media() does not report the format, so the bytes are all there is
    — and sending the wrong MIME type to a vision model is rejected at the API
    rather than degraded gracefully.
    """

    def test_jpeg(self):
        assert provider._sniff_image_mime(b"\xff\xd8\xff\xe0rest") == "image/jpeg"

    def test_png(self):
        assert provider._sniff_image_mime(b"\x89PNG\r\n\x1a\nrest") == "image/png"

    def test_webp(self):
        """RIFF....WEBP — the marker is at byte 8, not the start, so a prefix
        check alone would miss it."""
        assert provider._sniff_image_mime(b"RIFF\x00\x00\x00\x00WEBPrest") == "image/webp"

    def test_gif87a(self):
        assert provider._sniff_image_mime(b"GIF87a rest") == "image/gif"

    def test_gif89a(self):
        assert provider._sniff_image_mime(b"GIF89a rest") == "image/gif"

    def test_an_unknown_header_falls_back_to_jpeg(self):
        """Best effort beats refusing: the overwhelming majority really are
        JPEG, and a wrong guess fails no worse than not sending the image."""
        assert provider._sniff_image_mime(b"\x00\x01\x02\x03") == "image/jpeg"

    def test_empty_bytes_do_not_raise(self):
        """A zero-byte download is a real outcome, and an IndexError here
        would surface far from its cause."""
        assert provider._sniff_image_mime(b"") == "image/jpeg"
