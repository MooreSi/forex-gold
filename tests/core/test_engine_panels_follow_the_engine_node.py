"""The Signal Generator and Brain views show the node that is doing the work.

Owner, 2026-10-21: "when using the vps the 'does it beat chance', pro signal
model, ledger, virtual trades should be based on what the vps is doing so i can
understand how the engine is developing, same for breakout and trend pa ...
brain also needs the stats/details from the vps".

With the VPS the active trader and centralized generation off, the engines
analyse on the VPS only (node_roles.engines_generate_here). The React panels
read this Mac's own engines, stood down since the VPS took over, so every
number described a machine that was not generating. The VPS's stats broadcast
(remote_stats_facade) was only ever read by Trend PA.

Each panel now asks the engine node for the same report its own dashboard
would build, over the peer-report channel the Fill cost card uses. Rules:

  * engines on the peer = the VPS trades AND centralized generation is off.
    Under centralized generation the engines are HERE, and the VPS's are idle;
  * Brain follows the TRADER (its gates and decision log are the trader's),
    like Fill cost;
  * no fallback to this node's data when the peer cannot answer -- that is
    the misreport being fixed. One exception: Trend PA, whose broadcast copy
    IS the VPS's data, is used when the VPS runs code too old to answer.

Nothing here reaches a broker; the peer client is a recorder.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster import peer_reports as pr
from backend.src.services.cluster.remote_control import RemoteControlFailed



class _FakeClient:
    def __init__(self, reply=None, raises=None):
        self.reply, self.raises = reply, raises
        self.asked: list[tuple] = []
        self.remote_signal_gen_stats: dict = {}

    async def request_peer_report(self, name, args, **kw):
        self.asked.append((name, args))
        if self.raises:
            raise self.raises
        return self.reply


@pytest.fixture
def peer(monkeypatch):
    holder = {"client": _FakeClient(reply={"result": {"from": "vps"}})}
    monkeypatch.setattr(pr, "trader_is_peer", lambda: True)
    monkeypatch.setattr(pr, "_centralized", lambda: False)
    monkeypatch.setattr(pr._client, "get_instance", lambda: holder["client"])
    return holder


def _local(calls):
    async def build():
        calls.append("local")
        return {"from": "mac"}
    return build


class TestWhereTheEnginesAre:
    def test_on_the_peer_when_the_vps_trades_and_generation_is_not_centralized(
            self, monkeypatch):
        monkeypatch.setattr(pr, "trader_is_peer", lambda: True)
        monkeypatch.setattr(pr, "_centralized", lambda: False)
        assert pr.engines_on_peer() is True

    def test_here_under_centralized_generation(self, monkeypatch):
        monkeypatch.setattr(pr, "trader_is_peer", lambda: True)
        monkeypatch.setattr(pr, "_centralized", lambda: True)
        assert pr.engines_on_peer() is False

    def test_here_when_this_node_trades(self, monkeypatch):
        monkeypatch.setattr(pr, "trader_is_peer", lambda: False)
        monkeypatch.setattr(pr, "_centralized", lambda: False)
        assert pr.engines_on_peer() is False


class TestAPanelReport:
    @pytest.mark.asyncio
    async def test_the_vps_answers_and_is_labelled(self, peer):
        calls: list = []
        got = await pr.panel_from_engine_node("reversal_panel", _local(calls))

        assert got == {"from": "vps", "node": "remote"}
        assert peer["client"].asked == [("reversal_panel", {})]
        assert calls == [], "this node's idle engine was read anyway"

    @pytest.mark.asyncio

    async def test_this_node_answers_unchanged_when_the_engines_are_here(
            self, monkeypatch):
        monkeypatch.setattr(pr, "engines_on_peer", lambda: False)
        calls: list = []

        got = await pr.panel_from_engine_node("reversal_panel", _local(calls))

        assert got == {"from": "mac"}
        assert calls == ["local"]

    @pytest.mark.asyncio

    async def test_an_unreachable_vps_is_said_never_replaced_by_local_data(self, peer):
        peer["client"].raises = ConnectionError("not connected")
        calls: list = []

        with pytest.raises(RemoteControlFailed):
            await pr.panel_from_engine_node("breakout_panel", _local(calls))
        assert calls == []

    @pytest.mark.asyncio

    async def test_an_older_vps_is_told_to_update(self, peer):
        peer["client"].reply = {"error": "unknown report 'breakout_panel'"}

        with pytest.raises(RemoteControlFailed, match="older code"):
            await pr.panel_from_engine_node("breakout_panel", _local([]))


class TestRegistering:
    @pytest.mark.asyncio
    async def test_a_registered_report_can_be_run_by_name_and_takes_no_arguments(
            self, monkeypatch):
        monkeypatch.setattr(pr, "REPORTS", dict(pr.REPORTS))
        monkeypatch.setattr(pr, "_ARGS", dict(pr._ARGS))

        async def _panel():
            return {"ok": 1}

        pr.register("demo_panel", _panel)

        assert await pr.run_local("demo_panel", {}) == {"ok": 1}
        with pytest.raises(ValueError):
            await pr.run_local("demo_panel", {"limit": 5})

    def test_the_composition_root_registers_both_engine_panels(self):
        import backend.src.api.server  # noqa: F401  (registers on import)
        assert {"breakout_panel", "reversal_panel"} <= set(pr.REPORTS)


class TestBrain:
    @pytest.mark.asyncio
    async def test_brain_follows_the_trader(self, peer):
        got = await pr.brain_async(limit=40)

        assert peer["client"].asked == [("brain", {"limit": 40})]
        assert got["node"] == "remote"

    def test_brain_is_a_report_the_vps_will_run(self):
        assert "brain" in pr.REPORTS
        assert pr._ARGS["brain"]["limit"](60)
        assert not pr._ARGS["brain"]["limit"](10_000)


class TestTrendPa:
    @pytest.mark.asyncio
    async def test_the_vps_answers(self, peer):
        got = await pr.trend_pa_from_engine_node()

        assert peer["client"].asked == [("trend_pa_panel", {})]
        assert got["where"] == "remote"

    @pytest.mark.asyncio

    async def test_an_older_vps_falls_back_to_its_own_broadcast_copy(self, peer):
        """The broadcast IS the VPS's data, so this is not the misreport."""
        peer["client"].reply = {"error": "unknown report 'trend_pa_panel'"}
        peer["client"].remote_signal_gen_stats = {"trend_pa": {"live": {"n": 4}}}

        got = await pr.trend_pa_from_engine_node()

        assert got == {"live": {"n": 4}, "where": "remote"}

    @pytest.mark.asyncio

    async def test_an_older_vps_with_no_broadcast_says_so(self, peer):
        peer["client"].reply = {"error": "unknown report 'trend_pa_panel'"}

        with pytest.raises(RemoteControlFailed):
            await pr.trend_pa_from_engine_node()


class TestWhatTheVpsRuns:
    """The reports the VPS runs when the Mac asks: its own data, built the way
    its own dashboard builds it."""

    @pytest.mark.asyncio
    async def test_brain_is_this_nodes_feed_with_the_limit_asked_for(self, monkeypatch):
        from backend.src.services.brain import feed
        seen = []

        async def _snap(limit=60):
            seen.append(limit)
            return {"gates": [], "events": []}

        monkeypatch.setattr(feed, "snapshot_async", _snap)

        assert await pr.run_local("brain", {"limit": 25}) == {"gates": [], "events": []}
        assert seen == [25]

    @pytest.mark.asyncio
    async def test_trend_pa_is_this_nodes_local_report(self, monkeypatch):
        from backend.src.services.trend_pa import panel_data

        async def _thread(fn, *a):
            return fn(*a)

        monkeypatch.setattr(panel_data, "local_report", lambda: {"live": {"n": 2}})
        monkeypatch.setattr(pr.db_module, "to_db_thread", _thread)

        assert await pr.run_local("trend_pa_panel", {}) == {"live": {"n": 2}}


class TestCentralized:
    def test_reads_the_setting(self, monkeypatch):
        monkeypatch.setattr(pr.db_module, "get_risk_settings",
                            lambda: {"centralized_signal_gen_enabled": 1})
        assert pr._centralized() is True

    def test_an_unreadable_setting_is_not_centralized(self, monkeypatch):
        """Not centralized means "the VPS's engines are the live ones", so
        the panel asks the VPS -- and says so if it cannot answer, rather than
        quietly showing this node."""
        def _boom():
            raise RuntimeError("no db")
        monkeypatch.setattr(pr.db_module, "get_risk_settings", _boom)
        assert pr._centralized() is False


class TestAHeadlessVpsCanAnswer:
    """Owner, 2026-10-21, after updating the VPS: the Reversal panel still
    said "the VPS is running older code" while Settings > Remote showed both
    nodes on ed68af27. The VPS runs headless (run.py `_run_headless`), which
    never imports backend.src.api.server -- the only place the two panels
    were registered. Brain and Trend PA answered; they are in the table
    from the start. app.startup() is the composition root both boots share,
    so it offers them too."""

    def test_the_offer_registers_both_engine_panels(self, monkeypatch):
        monkeypatch.setattr(pr, "REPORTS", {k: v for k, v in pr.REPORTS.items()
                                            if not k.endswith("_panel") or k == "trend_pa_panel"})
        monkeypatch.setattr(pr, "_ARGS", dict(pr._ARGS))
        from backend.src import app as app_lifecycle

        app_lifecycle._offer_engine_panels_to_peer()

        assert {"breakout_panel", "reversal_panel"} <= set(pr.REPORTS)

    def test_startup_makes_the_offer(self):
        import ast
        import inspect
        from backend.src import app as app_lifecycle

        tree = ast.parse(inspect.getsource(app_lifecycle.startup))
        called = {n.func.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_offer_engine_panels_to_peer" in called
