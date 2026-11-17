"""The admin node's "new feedback" alert in the title bar.

Feedback that reaches the issuer is unread until the owner opens the alert's
popup; then it is read and the alert goes away.

What can go wrong quietly, and what these pin:

  * **Every old entry lights the alert up.** Rows stored before the alert
    existed carry no `read` field. They count as read.
  * **A customer install shows the owner's inbox.** Only the issuer has one;
    everywhere else the count is 0 and the list is empty.
  * **Feedback arriving while the popup is open is swallowed.** Marking read
    takes the ids that were shown, never "everything".
  * **The header poll falls over.** A broken store gives 0, never raises.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.feedback import notify, service, store

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def files(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "_PATH", tmp_path / "feedback.json")


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    async def ok(*a, **kw):
        return True

    async def mail_ok(*a, **kw):
        return True, ""
    monkeypatch.setattr(notify, "_send_telegram", ok)
    monkeypatch.setattr(notify, "_send_email", mail_ok)


def _entry(**kw):
    base = {"id": "a" * 32, "kind": "bug", "message": "It broke",
            "name": "Sam", "email": "s@x.co", "hostname": "mac-01",
            "version": "1.2.3", "submitted_at": 1000.0}
    return {**base, **kw}


class TestUnread:
    async def test_new_feedback_on_the_issuer_is_unread(self):
        await service.receive(_entry())

        assert service.unread_count(issuer=True) == 1
        (row,) = service.unread_feedback(issuer=True)
        assert row["id"] == "a" * 32
        assert row["message"] == "It broke" and row["name"] == "Sam"

    async def test_a_customer_install_has_no_inbox(self):
        await service.receive(_entry())

        assert service.unread_count(issuer=False) == 0
        assert service.unread_feedback(issuer=False) == []

    async def test_rows_from_before_the_alert_count_as_read(self, tmp_path):
        (tmp_path / "feedback.json").write_text(json.dumps([
            {**_entry(), "status": "open", "received_at": 1.0, "completed_at": 0},
        ]), encoding="utf-8")

        assert service.unread_count(issuer=True) == 0

    async def test_newest_first(self):
        await service.receive(_entry(id="1" * 32, submitted_at=1.0))
        await service.receive(_entry(id="2" * 32, submitted_at=2.0))

        assert [r["id"][0] for r in service.unread_feedback(issuer=True)] == ["2", "1"]

    async def test_a_broken_store_reads_as_nothing(self, tmp_path):
        (tmp_path / "feedback.json").write_text("{nope", encoding="utf-8")

        assert service.unread_count(issuer=True) == 0


class TestReading:
    async def test_marking_read_clears_the_alert(self):
        await service.receive(_entry())

        assert service.mark_read(["a" * 32], issuer=True) == 1

        assert service.unread_count(issuer=True) == 0

    async def test_only_the_ids_shown_are_marked(self):
        await service.receive(_entry(id="1" * 32))
        await service.receive(_entry(id="2" * 32))   # arrived while open

        service.mark_read(["1" * 32], issuer=True)

        assert [r["id"] for r in service.unread_feedback(issuer=True)] == ["2" * 32]

    async def test_a_customer_install_marks_nothing(self):
        await service.receive(_entry())

        assert service.mark_read(["a" * 32], issuer=False) == 0
        assert service.unread_count(issuer=True) == 1

    async def test_reading_does_not_complete_it(self):
        await service.receive(_entry())

        service.mark_read(["a" * 32], issuer=True)

        assert store.list_all()[0]["status"] == "open"
