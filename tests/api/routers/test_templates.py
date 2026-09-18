"""EA templates — rule sets the MetaTrader EA runs natively.

A template fully replaces a channel's normal strategy, so editing one changes
how future trades are managed. The assertion that matters is about the PUSH: a
template saved while no EA is connected is saved and will apply on the next
signal, which is a completely different outcome from a failed save and must
not read as one.

Nothing here reaches an EA: `push_template` is replaced with a recorder.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import templates as templates_router


@pytest.fixture
def ea(monkeypatch):
    state = {
        "templates": [{"name": "Grid Runner"}, {"name": "Trail Runner"}],
        "one": {"name": "Grid Runner", "anchor_pips": 12, "grid_legs": 3},
        "healthy": True,
        "last_seen": 3.2,
        "pushed": True,
        "writes": [],
    }
    monkeypatch.setattr(templates_router.broker_ctl, "list_ea_templates",
                        lambda: state["templates"])
    monkeypatch.setattr(templates_router.broker_ctl, "get_ea_template",
                        lambda name: state["one"] if name == state["one"]["name"] else None)
    monkeypatch.setattr(templates_router.broker_ctl, "save_ea_template",
                        lambda name, values: state["writes"].append(("save", name, values)))
    monkeypatch.setattr(templates_router.broker_ctl, "delete_ea_template",
                        lambda name: state["writes"].append(("delete", name)))
    monkeypatch.setattr(templates_router.broker_ctl, "install_builtin_template",
                        lambda: state["writes"].append(("install",)))
    monkeypatch.setattr(templates_router.broker_ctl, "push_template",
                        lambda name, values: state["writes"].append(("push", name)) or state["pushed"])
    monkeypatch.setattr(templates_router.broker_ctl, "ea_is_healthy", lambda: state["healthy"])
    monkeypatch.setattr(templates_router.broker_ctl, "ea_seconds_since_last_seen",
                        lambda: state["last_seen"])
    monkeypatch.setattr(templates_router.broker_ctl, "BUILTIN_PRESET_NAME", "Shipped Default")
    return state


def test_the_list_says_whether_an_ea_is_there_to_push_to(make_client, ea):
    body = make_client().get("/api/trading/templates").json()

    assert [t["name"] for t in body["templates"]] == ["Grid Runner", "Trail Runner"]
    assert body["ea_connected"] is True
    assert body["ea_last_seen_secs"] == 3.2
    assert body["builtin"] == "Shipped Default"


def test_a_silent_ea_is_reported_as_not_connected(make_client, ea):
    ea["healthy"] = False
    ea["last_seen"] = None

    body = make_client().get("/api/trading/templates").json()

    assert body["ea_connected"] is False
    assert body["ea_last_seen_secs"] is None


def test_one_template_is_returned_by_name(make_client, ea):
    body = make_client().get("/api/trading/templates/Grid Runner").json()

    assert body["grid_legs"] == 3


def test_an_unknown_template_is_a_named_404_not_an_empty_object(make_client, ea):
    """An empty object would render as a template with every setting at zero."""
    r = make_client().get("/api/trading/templates/No Such Thing")

    assert r.status_code == 404
    assert "No Such Thing" in r.json()["error"]["message"]


def test_saving_stores_the_values_and_reports_the_push(make_client, ea):
    body = make_client().put("/api/trading/templates/Grid Runner",
                             json={"anchor_pips": 20}).json()

    assert ("save", "Grid Runner", {"anchor_pips": 20}) in ea["writes"]
    assert ("push", "Grid Runner") in ea["writes"]
    assert body["pushed"] is True


def test_a_save_with_no_ea_connected_is_still_a_save(make_client, ea):
    """"pushed: false" means saved and will apply on the next signal. Reading
    that as a failed save is the mistake this reports its way out of."""
    ea["pushed"] = False

    body = make_client().put("/api/trading/templates/Grid Runner",
                             json={"anchor_pips": 20}).json()

    assert body["pushed"] is False
    assert ("save", "Grid Runner", {"anchor_pips": 20}) in ea["writes"]


def test_the_save_happens_before_the_push(make_client, ea):
    """Push first and a failed save leaves the EA running values that are not
    stored anywhere."""
    make_client().put("/api/trading/templates/Grid Runner", json={"anchor_pips": 20})

    kinds = [w[0] for w in ea["writes"]]
    assert kinds.index("save") < kinds.index("push")


def test_deleting_returns_what_is_left(make_client, ea):
    ea["templates"] = [{"name": "Trail Runner"}]

    body = make_client().delete("/api/trading/templates/Grid Runner").json()

    assert ("delete", "Grid Runner") in ea["writes"]
    assert [t["name"] for t in body["templates"]] == ["Trail Runner"]


def test_installing_the_builtin_preset_returns_the_new_list(make_client, ea):
    body = make_client().post("/api/trading/templates/install-builtin").json()

    assert ("install",) in ea["writes"]
    assert len(body["templates"]) == 2


def test_reading_a_template_never_pushes_it(make_client, ea):
    """Negative control: a poll that pushed would rewrite the EA's live
    settings every few seconds."""
    client = make_client()
    client.get("/api/trading/templates")
    client.get("/api/trading/templates/Grid Runner")

    assert ea["writes"] == []
