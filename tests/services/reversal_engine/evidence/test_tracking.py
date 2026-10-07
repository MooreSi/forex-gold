"""Mock HTTP transport only; no external writes."""
import asyncio
import httpx
from backend.src.services.reversal_engine.evidence.evidence_repo import EvidenceStore
from backend.src.services.reversal_engine.evidence.tracking import export_pending


def run():
    return {"id": "local-A", "created_at": 100, "env": "demo", "policy_hash": "policy-A",
            "dataset_hash": "dataset-A", "model_hash": None, "metrics": {"n": 2, "eligible": False},
            "params": {"label": "broker-net-R"}}


def test_mlflow_payload_and_export_ack_are_durable(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    store.experiment(run())
    requests = []
    def reply(request):
        requests.append(request)
        if request.url.path.endswith("get-by-name"):
            return httpx.Response(200, json={"experiment": {"experiment_id": "7"}})
        if request.url.path.endswith("search"):
            return httpx.Response(200, json={"runs": []})
        if request.url.path.endswith("create"):
            return httpx.Response(200, json={"run": {"info": {"run_id": "remote-A"}}})
        return httpx.Response(200, json={})
    async def perform():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            await export_pending(store, "http://localhost:5000", client)
    asyncio.run(perform())
    assert store.runs(pending=True) == []
    assert store.runs()[0]["remote_id"] == "remote-A"
    assert requests[-1].url.path.endswith("runs/update")


def test_failed_export_preserves_pending_run_and_reuses_remote_id(tmp_path):
    store = EvidenceStore(tmp_path / "e.db")
    store.experiment(run())
    store.remote("local-A", "remote-A")
    paths = []
    def reply(request):
        paths.append(request.url.path)
        return httpx.Response(503, json={})
    async def perform():
        async with httpx.AsyncClient(transport=httpx.MockTransport(reply)) as client:
            await export_pending(store, "http://localhost:5000", client)
    asyncio.run(perform())
    assert len(store.runs(pending=True)) == 1
    assert paths == ["/api/2.0/mlflow/runs/log-batch"]
