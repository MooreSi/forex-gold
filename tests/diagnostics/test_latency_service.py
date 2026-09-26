"""Settings > Latency: the hops of each checker, and the live probes (docs/todo/006).

Two properties matter more than the arithmetic:

  * a hop with no measurement says so -- it is never reported as 0 ms, which
    would read as "fast" to someone hunting a delay;
  * every probe is read-only and none of them can raise into the screen: a
    dead bridge is a probe result, not a 500.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.diagnostics import latency
from backend.src.utils import latency_trace as lt


@pytest.fixture(autouse=True)
def _clean():
    lt.clear()
    yield
    lt.clear()


def _trace(key, stages_ms: dict, pipeline=None, label=None):
    """Stamps at the given offsets (ms) on a synthetic monotonic line."""
    lt._traces[key] = {s: 1000.0 + ms / 1000.0 for s, ms in stages_ms.items()}
    lt._meta[key] = {"at": 1.0, **({"pipeline": pipeline} if pipeline else {}),
                     **({"label": label} if label else {})}


def _hop(view, hop_id):
    return next(h for h in view["hops"] if h["id"] == hop_id)


class TestTheTelegramChecker:
    def test_it_lists_every_hop_from_post_to_order(self):
        ids = [h["id"] for h in latency.pipeline_view("telegram")["hops"]]

        assert ids == ["delivery", "queue", "buffer", "pickup", "decide", "order", "total"]

    def test_each_hop_is_the_gap_between_its_two_stamps(self):
        _trace("1", {"t0_posted": 0, "t1_arrived": 800, "t3_dequeued": 802,
                     "t4_buffered": 803, "t6_scanning": 850, "t7_decided": 900,
                     "t8_ordered": 1400})

        view = latency.pipeline_view("telegram")

        assert _hop(view, "delivery")["stats"]["p50"] == 800.0
        assert _hop(view, "pickup")["stats"]["p50"] == 47.0
        assert _hop(view, "order")["stats"]["p50"] == 500.0
        assert _hop(view, "total")["stats"]["p50"] == 600.0

    def test_an_unmeasured_hop_is_empty_not_zero(self):
        _trace("1", {"t1_arrived": 0, "t3_dequeued": 2})

        assert _hop(latency.pipeline_view("telegram"), "order")["stats"] == {}

    def test_a_slow_hop_is_flagged(self):
        _trace("1", {"t6_scanning": 0, "t7_decided": 5000})

        assert _hop(latency.pipeline_view("telegram"), "decide")["slow"] is True
        assert _hop(latency.pipeline_view("telegram"), "queue")["slow"] is False

    def test_a_negative_first_hop_is_clamped(self):
        """Telegram's post time has 1 s resolution and this machine's clock is
        not Telegram's, so the first hop can come out a few ms below zero. It
        is shown as 0, never as a negative duration."""
        _trace("1", {"t0_posted": 300, "t1_arrived": 0})

        assert _hop(latency.pipeline_view("telegram"), "delivery")["stats"]["p50"] == 0.0

    def test_recent_signals_carry_each_hop(self):
        _trace("9", {"t1_arrived": 0, "t8_ordered": 700}, label="GOLD VIP")

        (row,) = latency.pipeline_view("telegram")["recent"]

        assert row["label"] == "GOLD VIP"
        assert row["hops"]["total"] == 700.0
        assert row["hops"]["order"] is None


class TestTheSignalGeneratorChecker:
    def test_engine_traces_are_kept_apart_from_telegram(self):
        _trace("bo:1", {"e1_created": 0, "e2_exec_start": 4000, "e3_ordered": 4600},
               pipeline="engine", label="Breakout BO-0001")

        view = latency.pipeline_view("engine")

        assert _hop(view, "order")["stats"]["p50"] == 600.0
        assert _hop(view, "trigger")["stats"]["p50"] == 4000.0
        assert latency.pipeline_view("telegram")["recent"] == []

    def test_the_polling_waits_are_stated(self):
        waits = {w["id"]: w for w in latency.structural_waits()}

        assert waits["breakout_cycle"]["seconds"] == 60
        assert waits["trigger_poll"]["seconds"] == 5


class _Engine:
    def __init__(self, health=None, tick=object(), fail=False):
        self._health = health if health is not None else {"connected": True}
        self._tick = tick
        self._fail = fail

    async def get_bridge_health(self):
        if self._fail:
            raise ConnectionError("bridge down")
        return self._health

    async def get_fresh_tick(self):
        return self._tick


class _Reader:
    async def api_round_trip(self):
        return {"ok": True, "ms": 42.0, "detail": "", "session_dc": 4, "nearest_dc": 4}


class TestProbes:
    @pytest.mark.asyncio
    async def test_every_probe_reports(self, monkeypatch):
        monkeypatch.setattr(latency, "_ea_instance", lambda: None)

        out = await latency.run_probes(_Engine(), _Reader())

        assert set(out) == {"loop", "db", "bridge", "tick", "ea", "telegram"}
        assert out["telegram"]["ms"] == 42.0
        assert out["ea"] == {"ok": False, "ms": None, "detail": "EA not connected",
                             "amber_ms": latency.PROBE_AMBER_MS["ea"]}

    @pytest.mark.asyncio
    async def test_a_dead_bridge_is_a_result_not_an_exception(self, monkeypatch):
        monkeypatch.setattr(latency, "_ea_instance", lambda: None)

        out = await latency.run_probes(_Engine(fail=True), None)

        assert out["bridge"]["ok"] is False and "bridge down" in out["bridge"]["detail"]
        assert out["telegram"]["ok"] is False

    @pytest.mark.asyncio
    async def test_a_bridge_that_answers_disconnected_is_not_ok(self, monkeypatch):
        monkeypatch.setattr(latency, "_ea_instance", lambda: None)

        out = await latency.run_probes(_Engine(health={"connected": False}, tick=None), None)

        assert out["bridge"]["ok"] is False
        assert out["tick"]["ok"] is False

    @pytest.mark.asyncio
    async def test_no_engine_means_no_bridge_probe(self, monkeypatch):
        monkeypatch.setattr(latency, "_ea_instance", lambda: None)

        out = await latency.run_probes(None, None)

        assert out["bridge"]["ok"] is False and out["bridge"]["detail"] == "no trading runtime"

    @pytest.mark.asyncio
    async def test_the_ea_probe_is_the_bridge_s_own_ping(self, monkeypatch):
        class _Ea:
            async def ping_ms(self, timeout=3.0):
                return {"ok": True, "ms": 210.0, "detail": ""}
        monkeypatch.setattr(latency, "_ea_instance", lambda: _Ea())

        out = await latency.run_probes(None, None)

        assert out["ea"]["ms"] == 210.0


class TestTheVps:
    @pytest.mark.asyncio
    async def test_an_unpaired_install_has_no_vps_section(self, monkeypatch):
        monkeypatch.setattr(latency, "_paired_client", lambda: None)

        assert await latency.vps_report() is None

    @pytest.mark.asyncio
    async def test_a_paired_install_reports_the_peer(self, monkeypatch):
        class _Client:
            async def probe_peer(self, timeout=20.0):
                return {"ok": True, "rtt_ms": 35.0, "detail": "", "remote": {"probes": {}}}
        monkeypatch.setattr(latency, "_paired_client", lambda: _Client())

        out = await latency.vps_report()

        assert out["rtt_ms"] == 35.0 and out["remote"] == {"probes": {}}


@pytest.mark.asyncio
async def test_the_full_check_has_both_checkers_and_the_broker(monkeypatch, tmp_path):
    monkeypatch.setattr(latency, "_ea_instance", lambda: None)
    monkeypatch.setattr(latency, "_paired_client", lambda: None)
    monkeypatch.setattr(latency._broker_log, "summary",
                        lambda **kw: {"available": False, "servers": {}})

    out = await latency.check(_Engine(), _Reader())

    assert set(out["local"]["pipelines"]) == {"telegram", "engine", "forwarded"}
    assert out["local"]["broker"]["available"] is False
    assert out["vps"] is None


def test_the_passive_view_says_whether_a_vps_is_paired(monkeypatch):
    """The tab draws the VPS section from this before any check has run."""
    monkeypatch.setattr(latency, "_paired_client", lambda: object())
    assert latency.passive_view()["paired"] is True

    monkeypatch.setattr(latency, "_paired_client", lambda: None)
    view = latency.passive_view()
    assert view["paired"] is False
    assert set(view["pipelines"]) == {"telegram", "engine", "forwarded"}
