"""The final M5 recheck reads completed bars. Order path is a recorder."""
from tests.breakout_signal.test_live_execute_kill_switches import lab, _run


def test_fill_time_momentum_check_drops_the_forming_m5_bar(lab):
    main, _ = _run(lab)
    assert lab["momentum_calls"][0][1] == 79
    assert len(main.opened) == 1


def test_attempt_refresh_and_broker_fill_are_distinct_observations(lab, monkeypatch):
    from backend.src.services.market import entry_contract
    seen = []
    monkeypatch.setattr(entry_contract, "observe", lambda *a, **k: seen.append(a))
    _run(lab)
    stages = {row[3]: row for row in seen}
    assert "dispatch_attempt" in stages
    assert stages["fill_recheck"][5] == {"f": 1.0}
    assert stages["broker_result"][4]["result"]["mt5_ticket"] == 55501
