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

    def _rename(old, new):
        if new == "Trail Runner":
            raise ValueError(f"There is already an EA template called {new!r}.")
        state["writes"].append(("rename", old, new))
        return {"template": {"name": new},
                "repointed": {"schedule": 2, "channel_assignments": 1,
                              "ai_recommendations": 0, "risk_settings": 1}}

    monkeypatch.setattr(templates_router.broker_ctl, "rename_ea_template", _rename)
    monkeypatch.setattr(templates_router.broker_ctl, "template_references",
                        lambda name: {"schedule": 2, "channel_assignments": 1,
                                      "ai_recommendations": 0, "risk_settings": 1})
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


# -- The field schema ---------------------------------------------------------

def test_the_schema_describes_the_fields(make_client, ea):
    body = make_client().get("/api/trading/templates/schema").json()
    names = {f["name"] for f in body["fields"]}

    assert "trail_mode" in names
    assert "sl_pips" in names


def test_the_schema_route_is_not_read_as_a_template_called_schema(make_client, ea):
    # FastAPI matches in definition order. Declared after "/{name}", this
    # request answers 404 for a template nobody asked for.
    assert make_client().get("/api/trading/templates/schema").status_code == 200


def test_a_choice_field_carries_its_values(make_client, ea):
    body = make_client().get("/api/trading/templates/schema").json()
    trail = next(f for f in body["fields"] if f["name"] == "trail_mode")

    assert trail["type"] == "choice"
    assert "step" in trail["choices"]


# -- Export and import --------------------------------------------------------
#
# Owner, 2026-09-21: "trading > ea template - needs buttons to import, export
# and delete ea templates". Delete was already here. The service half
# (`ea_templates.export_templates` / `import_templates`, with the envelope,
# the validate-everything-before-writing rule and the overwrite guard) has
# been in the tree since the NiceGUI app; only the HTTP surface and the
# buttons were never ported. These endpoints are that surface and nothing
# more -- no validation of their own, so there is exactly one implementation
# of what a valid export file is.

@pytest.fixture
def transfer(monkeypatch, ea):
    ea["exported"] = '{"format": "forex-ea-templates", "templates": []}'
    ea["imported"] = {"added": ["Grid Runner"], "replaced": [], "skipped": []}

    def _export(names=None):
        ea["writes"].append(("export", names))
        return ea["exported"]

    def _import(payload, *, overwrite=False):
        ea["writes"].append(("import", payload, overwrite))
        if isinstance(ea["imported"], Exception):
            raise ea["imported"]
        return ea["imported"]

    monkeypatch.setattr(templates_router.broker_ctl, "export_templates", _export)
    monkeypatch.setattr(templates_router.broker_ctl, "import_templates", _import)
    monkeypatch.setattr(templates_router.broker_ctl, "export_filename",
                        lambda prefix="ea_templates": "ea_templates_20260921_101500.eatpl.json")
    return ea


def test_export_returns_the_file_and_the_name_to_save_it_as(make_client, transfer):
    r = make_client().get("/api/trading/templates/export")

    assert r.status_code == 200
    assert r.json()["content"] == transfer["exported"]
    assert r.json()["filename"] == "ea_templates_20260921_101500.eatpl.json"


def test_export_with_no_names_exports_every_template(make_client, transfer):
    """The panel's button is "export all" -- names=None is what the service
    reads as that, and an empty list would export nothing at all."""
    make_client().get("/api/trading/templates/export")

    assert ("export", None) in transfer["writes"]


def test_export_can_be_narrowed_to_named_templates(make_client, transfer):
    make_client().get("/api/trading/templates/export?names=Grid Runner&names=Trail Runner")

    assert ("export", ["Grid Runner", "Trail Runner"]) in transfer["writes"]


def test_the_export_route_is_not_read_as_a_template_called_export(make_client, transfer):
    # Same trap as /schema: declared after "/{name}" this answers 404.
    assert make_client().get("/api/trading/templates/export").status_code == 200


def test_export_never_writes_anything(make_client, transfer):
    """Negative control -- an export that saved would be a way to corrupt a
    template by reading it."""
    make_client().get("/api/trading/templates/export")

    assert [w[0] for w in transfer["writes"]] == ["export"]


def test_import_adds_the_file_and_says_what_it_did(make_client, transfer):
    body = make_client().post("/api/trading/templates/import",
                              json={"content": "{...}"}).json()

    assert ("import", "{...}", False) in transfer["writes"]
    assert body["added"] == ["Grid Runner"]
    assert body["skipped"] == []


def test_import_does_not_overwrite_unless_asked(make_client, transfer):
    """A shared file must never silently clobber a locally tuned template.
    The default is the safe one, and this is what keeps it that way."""
    make_client().post("/api/trading/templates/import", json={"content": "{...}"})

    assert transfer["writes"][-1] == ("import", "{...}", False)


def test_import_overwrites_when_asked(make_client, transfer):
    make_client().post("/api/trading/templates/import",
                       json={"content": "{...}", "overwrite": True})

    assert transfer["writes"][-1] == ("import", "{...}", True)


def test_import_returns_the_new_list_so_the_page_need_not_ask_again(make_client, transfer):
    body = make_client().post("/api/trading/templates/import",
                              json={"content": "{...}"}).json()

    assert [t["name"] for t in body["templates"]] == ["Grid Runner", "Trail Runner"]


def test_a_file_that_is_not_an_export_is_a_refusal_naming_the_problem(
        make_client, transfer):
    """`import_templates` validates every template before writing any of
    them, so a bad file imports nothing. What it must not do is answer 500 --
    the operator picked the wrong file, and the page has to be able to say
    which file and why."""
    transfer["imported"] = ValueError("not an EA template export")

    r = make_client().post("/api/trading/templates/import", json={"content": "nope"})

    # 409 is this app's Refusal code -- "the backend said no and the reason is
    # meant for the user" -- not a 500 and not a validation error.
    assert r.status_code == 409
    assert "not an EA template export" in r.json()["error"]["message"]


def test_an_import_with_no_content_is_refused_before_the_service_sees_it(
        make_client, transfer):
    r = make_client().post("/api/trading/templates/import", json={"content": ""})

    assert r.status_code == 409
    assert [w for w in transfer["writes"] if w[0] == "import"] == []


# ── Renaming ─────────────────────────────────────────────────────────────────
# A strategy override is stored as the string `template:<name>` in the trading
# schedule, on a channel, in the AI's recommendation and in the global risk
# settings, and `template_for_channel` falls THROUGH a name that no longer
# resolves to the next route. So the thing worth asserting about this endpoint
# is not that the row moved -- it is that the move is REPORTED, because a
# rename silently changing which strategy a channel trades is the failure the
# whole feature is built around.


def test_renaming_reports_what_it_repointed(make_client, ea):
    client = make_client()

    r = client.post("/api/trading/templates/Grid Runner/rename",
                    json={"name": "Grid Runner v2"})

    assert r.status_code == 200
    assert r.json()["repointed"] == {
        "schedule": 2, "channel_assignments": 1,
        "ai_recommendations": 0, "risk_settings": 1}
    assert ("rename", "Grid Runner", "Grid Runner v2") in ea["writes"]


def test_renaming_onto_an_existing_name_is_refused_in_the_user_s_words(
        make_client, ea):
    client = make_client()

    r = client.post("/api/trading/templates/Grid Runner/rename",
                    json={"name": "Trail Runner"})

    assert r.status_code >= 400
    assert "already" in r.text
    assert not [w for w in ea["writes"] if w[0] == "rename"]


def test_a_rename_with_no_new_name_never_reaches_the_service(make_client, ea):
    """An empty name would otherwise be a template called "" that no override
    can ever name again."""
    client = make_client()

    r = client.post("/api/trading/templates/Grid Runner/rename",
                    json={"name": "   "})

    assert r.status_code >= 400


def test_the_references_endpoint_changes_nothing(make_client, ea):
    """It exists so the panel can warn BEFORE a rename. A read that renamed
    anything would be the opposite of that."""
    client = make_client()

    r = client.get("/api/trading/templates/Grid Runner/references")

    assert r.status_code == 200
    assert r.json()["references"]["schedule"] == 2
    assert ea["writes"] == []


def test_references_for_a_template_that_is_not_there(make_client, ea):
    client = make_client()

    r = client.get("/api/trading/templates/Ghost/references")

    assert r.status_code == 404
