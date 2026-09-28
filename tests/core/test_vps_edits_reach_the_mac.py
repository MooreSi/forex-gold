"""A change made ON the VPS reaches the Mac.

Every `_forward_*_over_sync` asked `client.get_instance()` first. That never
returns None -- it builds a SyncClient on first call -- so on the VPS the
server's broadcast branch was unreachable: the VPS's own change was queued on
a client that is never connected, and the Mac never heard of it.

The change that mattered (2026-09-28, reported live): the circuit breaker
tripped on the VPS after three losses. Its state lives in the synced risk
settings (`circuit_breaker_active_until`), and the Mac's header went on
reading "Trading Active" because the trip never left the VPS.

The rule: a process whose sync server is LISTENING is the VPS end of the link
and broadcasts; anything else proposes through its client. A server that was
built and then stopped does not count -- a Mac that once pressed "Make this
node a VPS" and stopped it again must keep proposing to its real VPS.

Nothing here places, closes or modifies a trade.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.src.services.channels import repo as channels_repo
from backend.src.services.cluster.sync import client as sync_client
from backend.src.services.cluster.sync import server as sync_server
from backend.src.services.risk import expert_params, risk_settings_repo
from backend.src.services.risk import schedule as trading_schedule
from backend.src.services.risk import strategy_params


def _sent(name):
    """A stand-in coroutine method that records it was the one sent."""
    def method(*_a, **_k):
        return ("sent", name)
    return method


# (module holding _schedule_coro, forward call, client method, server method)
FORWARDERS = [
    pytest.param(risk_settings_repo,
                 lambda: risk_settings_repo._forward_settings_over_sync(
                     {"circuit_breaker_active_until": 1.0}),
                 "propose_settings", "broadcast_settings", id="risk settings"),
    pytest.param(strategy_params,
                 strategy_params._forward_strategy_params_over_sync,
                 "propose_strategy_params", "broadcast_strategy_params",
                 id="strategy params"),
    pytest.param(expert_params, expert_params._forward_over_sync,
                 "propose_expert_params", "broadcast_expert_params",
                 id="expert params"),
    pytest.param(trading_schedule,
                 trading_schedule._forward_trading_schedule_over_sync,
                 "propose_trading_schedule", "broadcast_trading_schedule",
                 id="trading schedule"),
    pytest.param(channels_repo,
                 lambda: channels_repo._forward_channel_strategy_over_sync(
                     "Reversal Engine", "conservative", False),
                 "propose_channel_strategy", "broadcast_channel_strategy",
                 id="channel strategy"),
]


@pytest.fixture
def link(fresh_db, monkeypatch):
    """Both ends present, as they are on a real VPS: the client singleton
    exists (get_instance built it) and so does the server."""
    cli = MagicMock()
    srv = MagicMock()
    for _, _, c_meth, s_meth in (p.values for p in FORWARDERS):
        setattr(cli, c_meth, _sent(c_meth))
        setattr(srv, s_meth, _sent(s_meth))
    monkeypatch.setattr(sync_client, "get_instance", lambda: cli)
    monkeypatch.setattr(sync_server, "get_instance", lambda: srv)
    state = {"listening": False}
    monkeypatch.setattr(sync_server, "is_listening", lambda: state["listening"])
    return state


def _capture(monkeypatch, module):
    sent = []
    monkeypatch.setattr(module, "_schedule_coro", sent.append)
    return sent


@pytest.mark.parametrize("module, forward, client_method, server_method", FORWARDERS)
def test_on_the_vps_a_local_change_is_broadcast_to_the_mac(
        link, monkeypatch, module, forward, client_method, server_method):
    link["listening"] = True
    sent = _capture(monkeypatch, module)

    forward()

    assert sent == [("sent", server_method)]


@pytest.mark.parametrize("module, forward, client_method, server_method", FORWARDERS)
def test_on_the_mac_a_local_change_is_proposed_to_the_vps(
        link, monkeypatch, module, forward, client_method, server_method):
    link["listening"] = False
    sent = _capture(monkeypatch, module)

    forward()

    assert sent == [("sent", client_method)]


def test_a_breaker_trip_on_the_vps_is_broadcast(link, monkeypatch):
    """The reported case, end to end through the real breaker: three losses
    on the VPS trip it, and the new state goes out to the Mac."""
    from backend.src.services.risk import circuit_breaker_repo as cb

    risk_settings_repo.update_risk_settings(
        {"circuit_breaker_enabled": 1, "circuit_breaker_losses": 3,
         "circuit_breaker_cooldown_mins": 15}, _from_sync=True)
    link["listening"] = True
    sent = _capture(monkeypatch, risk_settings_repo)

    for _ in range(3):
        state = cb.record_live_trade_outcome(won=False)

    assert state["just_triggered"] is True
    assert ("sent", "broadcast_settings") in sent
    assert ("sent", "propose_settings") not in sent
