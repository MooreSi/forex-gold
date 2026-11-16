"""Every feedback test keeps the sent list in a tmp file.

`service.submit` records each id on this install's sent list (so the sender
can hear when it is marked Completed). A test that redirects only the store
and outbox would otherwise write to the real data directory -- which the
first run of this change did -- and the next `flush` would carry an ask
the test never set up.
"""
from __future__ import annotations

import pytest

from backend.src.services.feedback import sent


@pytest.fixture(autouse=True)
def _sent_list_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(sent, "_PATH", tmp_path / "feedback_sent.json")
