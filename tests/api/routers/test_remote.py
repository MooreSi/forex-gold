"""Settings > Remote Node: two roles, one link, and two switches that bite.

**Nothing here places an order and nothing here reaches a socket.** The sync
client and server are recorders; `request_model_snapshot` copies nothing. What
is being tested is what gets stored, what gets started, and what the operator
is told.

The assertions that matter:

  * **A token is never echoed back.** Both roles have one and both are
    write-only, like every other credential in this layer.
  * **A failed start is recorded as off.** A stored "enabled" with no listener
    has the next restart claim the VPS is accepting connections when it is not.
  * **Centralized signal generation says what it costs.** With it on, a VPS
    that loses this machine stops receiving signals and does not start
    generating its own. An operator who flicks it and shuts the laptop has
    stopped trading without meaning to, and a bare toggle would not say so.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import remote as remote_router


@pytest.fixture
def remote(monkeypatch):
    state = {
        "config": {"sync_server_enabled": "0", "sync_server_port": "8765",
                   "headless_mode_enabled": "0"},
        "risk": {"centralized_signal_gen_enabled": 0},
        "token": "vps-token",
        "client_config": ("10.0.0.5", 8765, "stored-client-token"),
        "link": {"conn_state": "disconnected", "last_error": "",
                 "remote_status": {"balance": 1000.0}, "remote_settings": {}},
        "server_running": False,
        "connected": False,
        "calls": [],
        "server_start_raises": None,
        "snapshot_raises": None,
    }

    async def _server_start(host, port, token, **kw):
        state["calls"].append(("server_start", host, port, token))
        if state["server_start_raises"]:
            raise state["server_start_raises"]
        state["server_running"] = True

    async def _server_stop():
        state["calls"].append(("server_stop",))
        state["server_running"] = False

    async def _snapshot(direction):
        state["calls"].append(("snapshot", direction))
        if state["snapshot_raises"]:
            raise state["snapshot_raises"]

    async def _restart(engine):
        state["calls"].append(("restart",))
        return "restarting"

    monkeypatch.setattr(remote_router.node_ctl, "get_app_config",
                        lambda k: state["config"].get(k))
    monkeypatch.setattr(remote_router.node_ctl, "set_app_config",
                        lambda k, v: state["config"].__setitem__(k, v))
    monkeypatch.setattr(remote_router.node_ctl, "get_sync_token",
                        lambda: state["token"])
    monkeypatch.setattr(remote_router.node_ctl, "get_risk_settings",
                        lambda: dict(state["risk"]))
    monkeypatch.setattr(remote_router.node_ctl, "update_risk_settings",
                        lambda f: state["risk"].update(f))
    monkeypatch.setattr(remote_router.node_ctl, "restart_app", _restart)
    monkeypatch.setattr(remote_router.sync_ctl, "load_config",
                        lambda: state["client_config"])
    monkeypatch.setattr(remote_router.sync_ctl, "link_state",
                        lambda: dict(state["link"]))
    monkeypatch.setattr(remote_router.sync_ctl, "server_is_running",
                        lambda: state["server_running"])
    monkeypatch.setattr(remote_router.sync_ctl, "cert_fingerprint",
                        lambda: "AA:BB:CC")
    monkeypatch.setattr(remote_router.sync_ctl, "server_start", _server_start)
    monkeypatch.setattr(remote_router.sync_ctl, "server_stop", _server_stop)
    monkeypatch.setattr(remote_router.sync_ctl, "configure",
                        lambda h, p, t: state["calls"].append(("configure", h, p, t)))
    monkeypatch.setattr(remote_router.sync_ctl, "start",
                        lambda h, p, t: state["calls"].append(("start", h, p, t)))
    monkeypatch.setattr(remote_router.sync_ctl, "stop",
                        lambda: state["calls"].append(("stop",)))
    monkeypatch.setattr(remote_router.sync_ctl, "is_connected",
                        lambda: state["connected"])
    monkeypatch.setattr(remote_router.sync_ctl, "request_model_snapshot", _snapshot)
    monkeypatch.setattr(remote_router.engines_ctl, "sub_engines",
                        lambda: (object(), None, object()))
    return state


def _message(res) -> str:
    return res.json()["error"]["message"]


# ── Secrets ──────────────────────────────────────────────────────────────────

def test_neither_token_reaches_the_browser(make_client, remote):
    body = make_client().get("/api/remote/state").text

    assert "vps-token" not in body
    assert "stored-client-token" not in body


def test_the_state_says_a_token_is_stored_without_saying_what(make_client, remote):
    body = make_client().get("/api/remote/state").json()

    assert body["server"]["token_set"] is True
    assert body["client"]["token_set"] is True


def test_an_unpaired_node_says_so(make_client, remote):
    remote["token"] = ""
    remote["client_config"] = ("", 0, "")

    body = make_client().get("/api/remote/state").json()

    assert body["server"]["token_set"] is False
    assert body["client"]["token_set"] is False


# ── The VPS role ─────────────────────────────────────────────────────────────

class TestAcceptingConnections:
    def test_enabling_it_starts_the_listener_with_the_stored_token(
        self, make_client, remote,
    ):
        make_client().put("/api/remote/server", json={"enabled": True, "port": 9000})

        started = [c for c in remote["calls"] if c[0] == "server_start"]
        assert len(started) == 1
        assert started[0][2] == 9000
        assert started[0][3] == "vps-token"
        assert remote["config"]["sync_server_port"] == "9000"

    def test_the_engines_reach_the_server_under_the_right_names(
        self, make_client, remote, monkeypatch,
    ):
        """`sub_engines()` returns (breakout, bounce, reversal) in a fixed
        order and `server_start` takes them by keyword. Unpacking them in the
        wrong order sends a paired node the wrong engine under the right name,
        and nothing downstream would notice.

        Bound against the REAL `server_start` signature, not the recorder's:
        a recorder takes `**kwargs` and would accept any spelling at all.
        """
        import inspect

        from backend.src.controllers import sync_controller as real_sync

        breakout, bounce, reversal = object(), object(), object()
        remote["engines"] = (breakout, bounce, reversal)
        monkey = remote["engine_kwargs"] = {}

        async def _record(host, port, token, **kw):
            monkey.update(kw)
            remote["server_running"] = True

        monkeypatch.setattr(remote_router.sync_ctl, "server_start", _record)
        monkeypatch.setattr(remote_router.engines_ctl, "sub_engines",
                            lambda: (breakout, bounce, reversal))

        make_client().put("/api/remote/server", json={"enabled": True})

        # The names the real function declares, in the order sub_engines hands
        # them over — so a swapped pair fails here rather than in production.
        inspect.signature(real_sync.server_start).bind(
            "host", 1, "tok", **monkey)
        assert monkey["breakout_engine"] is breakout
        assert monkey["bounce_engine"] is bounce
        assert monkey["re_engine"] is reversal

    def test_without_a_token_it_refuses_rather_than_listening(
        self, make_client, remote,
    ):
        """A listener with no token comes up and rejects every connection,
        which looks identical to a network problem from the other end."""
        remote["token"] = ""

        res = make_client().put("/api/remote/server", json={"enabled": True})

        assert res.status_code == 409
        assert "token first" in _message(res)
        assert remote["calls"] == []
        assert remote["config"]["sync_server_enabled"] == "0"

    def test_a_failed_start_is_recorded_as_off(self, make_client, remote):
        """A stored "enabled" with no listener has the next restart claim the
        VPS is accepting connections when it is not."""
        remote["server_start_raises"] = OSError("address already in use")

        res = make_client().put("/api/remote/server", json={"enabled": True})

        assert res.status_code == 409
        assert "address already in use" in _message(res)
        assert remote["config"]["sync_server_enabled"] == "0"

    def test_disabling_it_stops_the_listener(self, make_client, remote):
        remote["server_running"] = True

        body = make_client().put(
            "/api/remote/server", json={"enabled": False}).json()

        assert ("server_stop",) in remote["calls"]
        assert body["server"]["running"] is False
        assert remote["config"]["sync_server_enabled"] == "0"

    def test_disabling_it_never_needs_a_token(self, make_client, remote):
        """Negative control for the refusal above. Being unable to STOP
        listening because the token was rotated would be absurd."""
        remote["token"] = ""
        remote["server_running"] = True

        res = make_client().put("/api/remote/server", json={"enabled": False})

        assert res.status_code == 200
        assert ("server_stop",) in remote["calls"]


# ── The client role ──────────────────────────────────────────────────────────

class TestConnectingOut:
    def test_it_saves_and_dials_in_one_action(self, make_client, remote):
        """A stored address that was never dialled is the shape of bug where
        the operator believes they are paired and the link has never been up."""
        make_client().put("/api/remote/client", json={
            "host": " 10.0.0.9 ", "port": 9001, "token": " tok "})

        assert ("configure", "10.0.0.9", 9001, "tok") in remote["calls"]
        assert ("start", "10.0.0.9", 9001, "tok") in remote["calls"]

    def test_without_a_host_it_refuses(self, make_client, remote):
        res = make_client().put("/api/remote/client",
                                json={"host": "  ", "token": "tok"})

        assert res.status_code == 409
        assert remote["calls"] == []

    def test_a_blank_token_reconnects_with_the_stored_one(
        self, make_client, remote,
    ):
        """Disconnect then reconnect to the same VPS must not need the token
        pasted again (owner, 2026-09-25): switching between local and remote
        is routine. The same address is not re-configured, because configure()
        drops the certificate pin and a plain reconnect is not a re-pair."""
        res = make_client().put("/api/remote/client",
                                json={"host": "10.0.0.5", "port": 8765, "token": ""})

        assert res.status_code == 200
        assert remote["calls"] == [("start", "10.0.0.5", 8765, "stored-client-token")]

    def test_a_blank_token_with_a_new_address_saves_it_with_the_stored_token(
        self, make_client, remote,
    ):
        res = make_client().put("/api/remote/client",
                                json={"host": "10.0.0.9", "port": 9001, "token": ""})

        assert res.status_code == 200
        assert ("configure", "10.0.0.9", 9001, "stored-client-token") in remote["calls"]
        assert ("start", "10.0.0.9", 9001, "stored-client-token") in remote["calls"]

    def test_a_blank_token_with_none_stored_is_refused(self, make_client, remote):
        remote["client_config"] = ("10.0.0.5", 8765, "")
        res = make_client().put("/api/remote/client",
                                json={"host": "10.0.0.5", "token": ""})

        assert res.status_code == 409
        assert "token" in _message(res)
        assert remote["calls"] == []

    def test_disconnecting_stops_the_client(self, make_client, remote):
        make_client().post("/api/remote/client/disconnect")

        assert ("stop",) in remote["calls"]

    def test_the_peers_numbers_are_only_reported_while_connected(
        self, make_client, remote,
    ):
        """A balance left on screen from a link that has since dropped is a
        number the operator will act on."""
        assert make_client().get("/api/remote/state").json()["client"]["remote_status"] == {}

        remote["link"]["conn_state"] = "connected"

        body = make_client().get("/api/remote/state").json()
        assert body["client"]["remote_status"]["balance"] == 1000.0


# ── The two switches that bite ───────────────────────────────────────────────

class TestCentralizedSignalGeneration:
    def test_turning_it_on_says_what_it_costs(self, make_client, remote):
        body = make_client().put(
            "/api/remote/centralized-signals", json={"enabled": True}).json()

        assert remote["risk"]["centralized_signal_gen_enabled"] == 1
        assert "does not fall back" in body["note"]

    def test_turning_it_off_says_the_opposite(self, make_client, remote):
        remote["risk"]["centralized_signal_gen_enabled"] = 1

        body = make_client().put(
            "/api/remote/centralized-signals", json={"enabled": False}).json()

        assert remote["risk"]["centralized_signal_gen_enabled"] == 0
        assert "own signals again" in body["note"]


class TestHeadlessMode:
    def test_it_saves_and_restarts(self, make_client, remote):
        """A mode that only arrives when something else happens to restart the
        app is a mode nobody can reason about."""
        body = make_client().put("/api/remote/headless", json={"enabled": True}).json()

        assert remote["config"]["headless_mode_enabled"] == "1"
        assert ("restart",) in remote["calls"]
        assert body["note"] == "restarting"


# ── The model snapshot ───────────────────────────────────────────────────────

class TestTheModelSnapshot:
    def test_it_transfers_in_the_named_direction(self, make_client, remote):
        remote["connected"] = True

        body = make_client().post("/api/remote/model-snapshot",
                                  json={"direction": "upload"}).json()

        assert ("snapshot", "upload") in remote["calls"]
        assert "upload complete" in body["note"]

    def test_a_direction_it_does_not_know_is_refused(self, make_client, remote):
        """It overwrites the destination's models. A typo must not pick one."""
        remote["connected"] = True

        res = make_client().post("/api/remote/model-snapshot",
                                 json={"direction": "sideways"})

        assert res.status_code == 400
        assert remote["calls"] == []

    def test_with_no_link_it_refuses(self, make_client, remote):
        res = make_client().post("/api/remote/model-snapshot",
                                 json={"direction": "download"})

        assert res.status_code == 409
        assert remote["calls"] == []

    def test_a_failed_transfer_says_so_rather_than_reporting_success(
        self, make_client, remote,
    ):
        """Half a model set, reported as complete, is a node that trades on
        weights it did not finish receiving."""
        remote["connected"] = True
        remote["snapshot_raises"] = OSError("connection reset")

        res = make_client().post("/api/remote/model-snapshot",
                                 json={"direction": "download"})

        assert res.status_code == 409
        assert "connection reset" in _message(res)


# ── Sending is never a GET ───────────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/api/remote/client/disconnect",
    "/api/remote/model-snapshot",
])
def test_an_action_is_not_reachable_by_a_get(path, make_client, remote):
    assert make_client().get(path).status_code == 405
    assert remote["calls"] == []


# ── Being reachable (2026-09-25) ─────────────────────────────────────────────

def test_the_state_says_how_the_other_machine_reaches_this_one(
    make_client, remote, monkeypatch,
):
    """A fresh VPS said "listening" and nothing else: no address to dial, no
    word on the firewall, nothing on what protects the link. The check is
    asked about the port this machine actually listens on."""
    asked = []

    def _reach(port):
        asked.append(port)
        return {"addresses": ["38.253.124.25"], "behind_nat": False,
                "firewall": "missing", "firewall_command": "netsh ...",
                "security": "TLS and a token"}

    monkeypatch.setattr(remote_router.node_ctl, "server_reachability", _reach)
    remote["config"]["sync_server_port"] = "9001"

    body = make_client().get("/api/remote/state").json()

    assert asked == [9001]
    assert body["server"]["reachability"]["addresses"] == ["38.253.124.25"]
    assert body["server"]["reachability"]["firewall"] == "missing"


# ── Make this node a VPS / Stop being a VPS (owner, 2026-09-25) ──────────────
# One press does what a VPS needs: a pairing token if there is none, the sync
# port open in the Windows firewall, and the listener started. The installer no
# longer opens that port, because most Windows installs are a main PC.

@pytest.fixture
def vps(remote, monkeypatch):
    remote["firewall"] = []
    remote["firewall_answer"] = "open"
    remote["generated"] = []

    async def _open(port):
        remote["firewall"].append(("open", port))
        return remote["firewall_answer"]

    async def _close(port):
        remote["firewall"].append(("close", port))
        return remote["firewall_answer"]

    def _generate():
        remote["token"] = "fresh-token"
        remote["generated"].append(1)
        return "fresh-token"

    monkeypatch.setattr(remote_router.node_ctl, "open_firewall", _open)
    monkeypatch.setattr(remote_router.node_ctl, "close_firewall", _close)
    monkeypatch.setattr(remote_router.node_ctl, "generate_sync_token", _generate)
    monkeypatch.setattr(remote_router.node_ctl, "server_reachability",
                        lambda port: {"addresses": [], "firewall": "open"})
    return remote


class TestMakingThisNodeAVps:
    def test_it_opens_the_port_and_starts_the_listener(self, make_client, vps):
        body = make_client().post("/api/remote/make-vps", json={"port": 8765}).json()

        assert vps["firewall"] == [("open", 8765)]
        assert ("server_start", ) == vps["calls"][-1][:1]
        assert vps["config"]["sync_server_enabled"] == "1"
        assert body["server"]["enabled"] is True

    def test_with_no_token_it_makes_one_and_shows_it_once(self, make_client, vps):
        vps["token"] = ""
        client = make_client()

        body = client.post("/api/remote/make-vps", json={"port": 8765}).json()

        assert body["token"] == "fresh-token"
        assert vps["calls"][-1] == ("server_start", vps["calls"][-1][1], 8765, "fresh-token")
        assert "fresh-token" not in client.get("/api/remote/state").text

    def test_an_existing_token_is_kept(self, make_client, vps):
        """A new one would silently unpair the machine already using the old."""
        body = make_client().post("/api/remote/make-vps", json={"port": 8765}).json()

        assert vps["generated"] == []
        assert "token" not in body

    def test_a_declined_windows_prompt_still_sets_it_up_and_says_so(self, make_client, vps):
        vps["firewall_answer"] = "declined"

        body = make_client().post("/api/remote/make-vps", json={"port": 8765}).json()

        assert vps["config"]["sync_server_enabled"] == "1"
        assert "not open" in body["note"]

    def test_a_listener_that_fails_to_start_is_refused(self, make_client, vps):
        vps["server_start_raises"] = OSError("address in use")

        res = make_client().post("/api/remote/make-vps", json={"port": 8765})

        assert res.status_code == 409
        assert vps["config"]["sync_server_enabled"] == "0"

    def test_it_is_not_reachable_by_a_get(self, make_client, vps):
        assert make_client().get("/api/remote/make-vps").status_code == 405
        assert vps["firewall"] == []


class TestStoppingBeingAVps:
    def test_it_stops_the_listener_and_closes_the_port(self, make_client, vps):
        vps["config"]["sync_server_enabled"] = "1"

        body = make_client().post("/api/remote/stop-vps", json={}).json()

        assert ("server_stop",) in vps["calls"]
        assert vps["firewall"] == [("close", 8765)]
        assert vps["config"]["sync_server_enabled"] == "0"
        assert body["server"]["enabled"] is False

    def test_the_token_is_kept_for_a_later_return(self, make_client, vps):
        make_client().post("/api/remote/stop-vps", json={})

        assert vps["token"] == "vps-token"


def test_keeping_an_existing_token_says_how_to_get_one(make_client, vps):
    """2026-09-25: a VPS kept its 6.1-era token, so nothing was shown, and the
    certificate fingerprint went into the other machine's token box instead."""
    body = make_client().post("/api/remote/make-vps", json={"port": 8765}).json()

    assert "New pairing token" in body["note"]
