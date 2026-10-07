"""Durable local experiments and optional MLflow REST outbox, no SDK needed."""
from __future__ import annotations
import asyncio
import math
import time
import httpx

EXPERIMENT = "forex-gold-broker-candidates"


async def export_pending(store, uri, client):
    root = uri.rstrip("/") + "/api/2.0/mlflow/"
    for record in await asyncio.to_thread(store.runs, True):
        run, remote_id = record["payload"], record["remote_id"]
        try:
            if not remote_id:
                response = await client.get(root + "experiments/get-by-name", params={"experiment_name": EXPERIMENT})
                if response.status_code == 404:
                    response = await client.post(root + "experiments/create", json={"name": EXPERIMENT})
                    if response.status_code == 400:  # concurrent creator: retry name lookup
                        response = await client.get(root + "experiments/get-by-name", params={"experiment_name": EXPERIMENT})
                response.raise_for_status()
                data = response.json()
                experiment_id = str(data.get("experiment_id") or data["experiment"]["experiment_id"])
                response = await client.post(root + "runs/search", json={"experiment_ids": [experiment_id],
                    "filter": "tags.evidence_run_id = '" + run["id"] + "'", "max_results": 1})
                response.raise_for_status()
                matches = response.json().get("runs", [])
                if matches:
                    remote_id = matches[0]["info"]["run_id"]
                else:
                    response = await client.post(root + "runs/create", json={"experiment_id": experiment_id,
                        "start_time": int(run["created_at"] * 1000),
                        "tags": [{"key": "evidence_run_id", "value": run["id"]}]})
                    response.raise_for_status()
                    remote_id = response.json()["run"]["info"]["run_id"]
                await asyncio.to_thread(store.remote, run["id"], remote_id)
            metrics = [{"key": k, "value": float(v), "timestamp": int(run["created_at"] * 1000), "step": 0}
                       for k, v in run["metrics"].items() if isinstance(v, (float, int)) and math.isfinite(float(v))]
            tags = [{"key": k, "value": str(run.get(k) or "unavailable")}
                    for k in ("env", "policy_hash", "dataset_hash", "model_hash", "code_hash", "schema_hash")]
            tags.append({"key": "mode", "value": "shadow; manual review required; never auto-promoted"})
            response = await client.post(root + "runs/log-batch", json={"run_id": remote_id, "metrics": metrics,
                "params": [{"key": k, "value": str(v)} for k, v in run["params"].items()], "tags": tags})
            response.raise_for_status()
            response = await client.post(root + "runs/update", json={"run_id": remote_id, "status": "FINISHED",
                                          "end_time": int(time.time() * 1000)})
            response.raise_for_status()
            await asyncio.to_thread(store.remote, run["id"], remote_id, True)
            await asyncio.to_thread(store.health, "mlflow", "ok", "experiment exported", time.time())
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            # Exception text can contain credential-bearing URLs: retain only type.
            await asyncio.to_thread(store.health, "mlflow", "unavailable", type(exc).__name__, time.time())
            break  # bounded retry on the next worker pass
