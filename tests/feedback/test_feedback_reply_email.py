"""The sender can give an email address so the owner can reply directly.

Optional. When given it replaces the install's stored email on the entry, so
every place the owner reads feedback (Telegram, email, admin console, the
new-feedback popup) shows the address the sender asked to be answered at.

What can go wrong quietly, and what these pin:

  * **A typo the owner cannot reply to.** An address that is not shaped like
    one is refused with a reason the sender can fix, before anything queues.
  * **Leaving it blank loses the stored email.** Blank keeps the identity's.
  * **The owner has to copy it by hand.** The alert email links it.
"""
from __future__ import annotations

import pytest

from backend.src.services.feedback import notify, outbox, service, store

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_PATH", tmp_path / "feedback.json")
    monkeypatch.setattr(outbox, "_PATH", tmp_path / "outbox.json")
    monkeypatch.setattr(service, "_identity",
                        lambda: {"name": "Sam", "email": "licence@x.co",
                                 "hostname": "mac-01", "version": "1"})


class TestTheReplyAddress:
    async def test_a_given_address_is_on_the_queued_entry(self):
        service.submit("bug", "It broke", email="sam@reply.co", issuer=False)

        assert outbox.pending()[0]["email"] == "sam@reply.co"

    async def test_it_is_trimmed(self):
        e = service.submit("bug", "It broke", email="  sam@reply.co  ", issuer=False)

        assert e["email"] == "sam@reply.co"

    async def test_blank_keeps_the_stored_email(self):
        e = service.submit("bug", "It broke", email="   ", issuer=False)

        assert e["email"] == "licence@x.co"

    @pytest.mark.parametrize("bad", ["sam", "sam@", "@x.co", "sam@x", "sa m@x.co",
                                     "a@b.c" + "o" * 300])
    async def test_a_malformed_address_is_refused_and_nothing_queues(self, bad):
        with pytest.raises(ValueError, match="email"):
            service.submit("bug", "It broke", email=bad, issuer=False)

        assert outbox.pending() == []


class TestTheOwnersEmail:
    async def test_the_address_is_a_mailto_link(self):
        body = notify._email_body({"kind": "bug", "message": "m",
                                   "email": "sam@reply.co", "submitted_at": 0})

        assert 'href="mailto:sam@reply.co"' in body

    async def test_html_in_the_address_cannot_break_out(self):
        body = notify._email_body({"kind": "bug", "message": "m",
                                   "email": '"><script>x</script>', "submitted_at": 0})

        assert "<script>" not in body
