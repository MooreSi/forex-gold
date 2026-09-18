"""The pre-boot licence screens, served without a UI framework.

These run BEFORE the app starts and are the only way back into a stranded
install, which sets the bar for every test here: the screen must render when
nothing else works. No `frontend/dist`, no database, no bridge, no browser
bundle — one HTML document this process can produce on its own.

**It grants nothing.** The only route that can store a licence goes through
`activation.activate_manually`, which verifies first;
`test_no_route_here_can_grant_a_licence` asserts no other route writes.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from backend.src.config.licence import activation, activation_server


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(activation, "verify_licence_key", lambda *a: False)
    return TestClient(activation_server.build_app("machine-abc123"),
                      raise_server_exceptions=False)


@pytest.fixture
def remote(monkeypatch):
    """The remote client, scripted. No socket is opened.

    Patches the REAL module's attributes rather than replacing the module in
    `sys.modules`. A substitute module passes in isolation and fails in a full
    run, because whichever test imported the real one first wins — and the
    failure surfaces in whichever file happens to run second, which is a
    miserable thing to debug.
    """
    from backend.src.services.cluster.remote import client as _rc

    state = {"status": {}, "activated": False, "requests": []}

    monkeypatch.setattr(_rc, "get_status", lambda: state["status"])
    monkeypatch.setattr(_rc, "request_registration",
                        lambda email, nickname: state["requests"].append((email, nickname)))
    monkeypatch.setattr(
        _rc, "licence_activated",
        type("E", (), {"is_set": staticmethod(lambda: state["activated"])})())
    return state


# ── It renders with nothing else working ─────────────────────────────────────

def test_the_form_renders_with_no_bundle_and_no_database(client):
    """A licence screen that needs the dashboard to have been compiled is a
    licence screen that cannot rescue a broken install."""
    r = client.get("/")

    assert r.status_code == 200
    assert "Licence activation required" in r.text
    assert "machine-abc123" in r.text


def test_a_wrong_url_shows_the_form_rather_than_a_404(client):
    """This is the only page on this process. A bare 404 reads as a broken
    install to somebody already locked out."""
    r = client.get("/settings")

    assert r.status_code == 200
    assert "Licence activation required" in r.text


def test_a_notice_explains_why_activation_is_being_asked_for_again(monkeypatch):
    monkeypatch.setattr(activation, "verify_licence_key", lambda *a: False)
    app = activation_server.build_app("machine-abc123",
                                      notice="Your licence expired on 2026-09-01.")

    r = TestClient(app).get("/")

    assert "Your licence expired on 2026-09-01." in r.text


def test_the_machine_id_is_escaped_not_injected(monkeypatch):
    """It comes from a hardware fingerprint, but it is still substituted into
    HTML, and a screen that renders markup from data is a habit not worth
    having on the one page that runs before every check."""
    monkeypatch.setattr(activation, "verify_licence_key", lambda *a: False)
    app = activation_server.build_app("<script>alert(1)</script>")

    r = TestClient(app).get("/")

    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;" in r.text


# ── The error screen ─────────────────────────────────────────────────────────

def test_the_error_screen_says_why_and_offers_nothing(monkeypatch):
    app = activation_server.build_app("", error="This licence is for another machine.")
    c = TestClient(app)

    r = c.get("/")

    assert "This licence is for another machine." in r.text
    assert "Request registration" not in r.text
    # 405, not a refusal: the activation routes are not registered on this app
    # at all. There is no form, and no endpoint behind the form that is not
    # there — a licence this install cannot have is not a thing to retry.
    assert c.post("/api/activation/manual", json={}).status_code == 405


def test_every_path_on_the_error_screen_shows_the_error(monkeypatch):
    app = activation_server.build_app("", error="Expired.")

    assert "Expired." in TestClient(app).get("/anything/at/all").text


# ── Registration ─────────────────────────────────────────────────────────────

def test_a_registration_request_is_forwarded_to_the_remote_client(client, remote):
    r = client.post("/api/activation/register",
                    json={"nickname": " Mac ", "email": " a@b.com "})

    assert r.json()["ok"] is True
    assert remote["requests"] == [("a@b.com", "Mac")]


def test_incomplete_details_never_reach_the_remote_client(client, remote):
    r = client.post("/api/activation/register", json={"nickname": "M", "email": "a@b.com"})

    assert r.json()["ok"] is False
    assert remote["requests"] == []


def test_the_screen_does_not_claim_a_request_was_sent(client, remote):
    """`request_registration` only QUEUES a connection. Saying "sent" here is
    the 2026-08-07 bug."""
    r = client.post("/api/activation/register",
                    json={"nickname": "Mac", "email": "a@b.com"})

    assert "Contacting" in r.json()["message"]
    assert "awaiting" not in r.json()["message"].lower()


def test_delivery_reports_what_the_client_actually_did(client, remote):
    remote["status"] = {"registration_sent_at": 1_750_000_000}

    assert client.get("/api/activation/delivery").json()["state"] == "delivered"


def test_delivery_reports_a_failure_as_a_failure(client, remote):
    remote["status"] = {"last_error": "connection refused"}

    body = client.get("/api/activation/delivery").json()

    assert body["state"] == "retrying"
    assert "connection refused" in body["message"]


# ── Manual activation ────────────────────────────────────────────────────────

def test_no_route_here_can_grant_a_licence(client, remote, monkeypatch):
    """Every route, with verification refusing. Nothing may be stored."""
    saved = []
    import backend.src.config.licence.store as _store

    monkeypatch.setattr(_store, "save", lambda record: saved.append(record))

    client.get("/")
    client.get("/licence-activated")
    client.get("/api/activation/status")
    client.get("/api/activation/delivery")
    client.post("/api/activation/register", json={"nickname": "Mac", "email": "a@b.com"})
    client.post("/api/activation/manual",
                json={"nickname": "Mac", "email": "a@b.com", "code": "FORGED"})

    assert saved == []


def test_a_verified_key_is_accepted(client, monkeypatch):
    saved = []
    import backend.src.config.licence.store as _store

    monkeypatch.setattr(_store, "save", lambda record: saved.append(record))
    monkeypatch.setattr(activation, "verify_licence_key", lambda *a: True)

    body = client.post("/api/activation/manual", json={
        "nickname": "Mac", "email": "a@b.com", "code": "KEY|2027-01-01",
    }).json()

    assert body["ok"] is True
    assert saved[0]["machine_id"] == "machine-abc123"


# ── The hand-off to the restarted app ────────────────────────────────────────

def test_the_status_endpoint_reports_a_pushed_licence(client, remote):
    remote["activated"] = True

    assert client.get("/api/activation/status").json()["activated"] is True


def test_the_wait_page_polls_the_probe_this_process_answers(client):
    """The probe is registered HERE and nowhere else: the real app 404ing it is
    the whole signal that the restart has happened."""
    page = client.get("/licence-activated").text

    assert activation_server.PROBE_PATH in page
    assert client.get(activation_server.PROBE_PATH).json() == {"stage": "activation"}


def test_the_probe_is_not_registered_by_the_real_app():
    """The other half of the same signal, asserted against the real app."""
    from backend.src.api.server import build_app as build_real

    real = build_real(engine_provider=lambda: None, install_auth_gate=False)
    with TestClient(real, raise_server_exceptions=False) as c:
        assert c.get(activation_server.PROBE_PATH).status_code == 404


# ── The agents brought up behind the screen ──────────────────────────────────

def test_a_failing_agent_does_not_take_the_screen_down(monkeypatch):
    """This screen is the only way back into a stranded install; an exception
    here replaces it with a traceback."""
    monkeypatch.setattr(activation, "verify_licence_key", lambda *a: False)

    def _boom():
        raise RuntimeError("no admin server")

    ran = []
    app = activation_server.build_app(
        "machine-abc123", on_startup=[_boom, lambda: ran.append("second")])

    with TestClient(app) as c:
        assert c.get("/").status_code == 200

    assert ran == ["second"], "a failing agent stopped the next one"


def test_the_admin_machine_starts_its_server_and_not_its_client():
    """"The admin Mac does not connect to itself, and doing both would have it
    dial its own port and log a refusal on every retry.\""""
    from backend.src.config.licence import guard

    agents = guard._agents_for_activation(is_admin=True, known_client=True)

    names = [getattr(f, "__name__", "") for f in agents]
    assert names == ["_autostart_admin_server", "_autostart_activation_agents"]


def test_a_known_client_connects_so_a_pushed_licence_can_land(self=None):
    from backend.src.config.licence import guard

    agents = guard._agents_for_activation(is_admin=False, known_client=True)

    assert [getattr(f, "__name__", "") for f in agents] == ["_autoconnect_known_client"]


def test_a_first_install_starts_nothing():
    """Negative control: it has no token, so there is nothing for it to connect
    with and nothing to issue itself."""
    from backend.src.config.licence import guard

    assert guard._agents_for_activation(is_admin=False, known_client=False) == []
