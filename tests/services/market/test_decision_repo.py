"""Append-only observations live in the caller's isolated engine database."""
from backend.src.services.market import decision_repo as repo
from backend.src.services.market import entry_contract as c
from backend.src.services.trend_pa import repo as tpa


def test_observations_are_append_only_and_readable(tmp_path):
    tpa.init(str(tmp_path / "tpa.db"))
    try:
        one = c.snapshot("trend_pa", "fill", {}, {"entry": 100}, {}, 10)
        repo.append(tpa.get_db(), "TPA-1", one)
        repo.append(tpa.get_db(), "TPA-1", {**one, "observed_at": 11})
        rows = repo.for_signal(tpa.get_db(), "TPA-1")
        assert len(rows) == 2
        assert [r["observed_at"] for r in rows] == [10, 11]
        assert rows[0]["context"]["entry"] == 100
    finally:
        tpa.close_db()
