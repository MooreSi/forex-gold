"""The merge: export, import, decide, and the Mac's two-way run (docs/todo/026).

Started from the Mac's Settings > Remote, because only the Mac can open a
request to the other node. One run:

  1. pull the VPS's own rows, page by page (peer report `ml_history`);
  2. push this node's own rows to the VPS, in frames under 256 KB;
  3. ask the VPS to run its benefit check, then run this node's.

A node exports only its own rows, never what it imported, so a second run
re-sends nothing it received and every import is `INSERT OR IGNORE`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from backend.src.services.ml_merge import benefit, kinds, repo, transfer

log = logging.getLogger(__name__)

PAGE = 500
_MAX_ROWS_PER_IMPORT = 5000

_run: dict = {"running": False, "stage": "", "started_at": None,
              "finished_at": None, "result": None, "error": None}


class MergeFailed(RuntimeError):
    pass


def own_node_id() -> str:
    from backend.src.db import database as db_module
    return db_module.get_or_create_node_id()


def export_page(kind: str, after: int, limit: int) -> dict:
    """A page of this node's own rows of `kind`, with the cursor for the next."""
    if kind not in repo.KINDS:
        raise ValueError(f"unknown training dataset {kind!r}")
    rows = kinds.export_page(kind, int(after), int(limit))
    return {"kind": kind, "rows": rows,
            "next_after": rows[-1]["id"] if rows else int(after),
            "done": len(rows) < int(limit)}


async def history_report(kind: str, after: int = 0, limit: int = PAGE) -> dict:
    """The peer report: a page, and which node it came from."""
    from backend.src.db import database as db_module
    page = await asyncio.to_thread(export_page, kind, after, limit)
    return {**page, "node_id": await db_module.to_db_thread(own_node_id)}


def import_rows(kind: str, origin_node: Any, rows: Any,
                own_node: Optional[str] = None) -> int:
    """Store the peer's rows of `kind`. Returns how many were new."""
    if kind not in repo.KINDS:
        raise ValueError(f"unknown training dataset {kind!r}")
    if not isinstance(origin_node, str) or not origin_node:
        raise ValueError("rows must name the node they came from")
    if not isinstance(rows, list) or len(rows) > _MAX_ROWS_PER_IMPORT:
        raise ValueError("rows must be a list of at most "
                         f"{_MAX_ROWS_PER_IMPORT}")
    own = own_node if own_node is not None else own_node_id()
    if origin_node == own:
        raise ValueError("these rows came from this node")
    key = kinds.ORDER[kind]
    triples = [(r["id"], key(r), r) for r in rows
               if isinstance(r, dict) and isinstance(r.get("id"), int)
               and not isinstance(r.get("id"), bool)]
    return repo.insert_rows(kind, origin_node, triples)


def evaluate_all() -> dict:
    return {kind: benefit.evaluate(kind) for kind in repo.KINDS}


def _retrain(kind: str) -> None:
    """Refit `kind`'s model now, so a verdict takes effect without waiting for
    the next five closes or the 22:00 retrain."""
    try:
        if kind == "re":
            from backend.src.services.reversal_engine import ml_engine
            ml_engine._request_retrain()
        elif kind == "bo":
            from backend.src.services.breakout_signal import ml_engine as bo_ml
            bo_ml._retrain()
        elif kind == "pro":
            from backend.src.services.reversal_engine import pro_model
            pro_model.fit_in_background(force=True)
    except Exception as exc:
        log.warning("[ml_merge] %s retrain after the merge failed: %s", kind, exc)


async def apply_async() -> dict:
    """Run every benefit check off the loop, then retrain what changed."""
    before = {k: repo.use_imported(k) for k in repo.KINDS}
    reports = await asyncio.to_thread(evaluate_all)
    for kind, report in reports.items():
        if report["use"] or before[kind]:
            _retrain(kind)
    return reports


async def _pull(client, own: str) -> dict:
    pulled = {}
    for kind in repo.KINDS:
        after, added, peer_node = 0, 0, None
        while True:
            ack = await client.request_peer_report(
                "ml_history", {"kind": kind, "after": after, "limit": PAGE}, timeout=60.0)
            if (ack or {}).get("error"):
                if "unknown report" in ack["error"]:
                    raise MergeFailed("The VPS is running older code that cannot merge. "
                                      "Update it (Settings > Remote), then try again.")
                raise MergeFailed(f"The VPS refused: {ack['error']}")
            res = ack.get("result") or {}
            peer_node = res.get("node_id")
            added += await asyncio.to_thread(import_rows, kind, peer_node,
                                             res.get("rows") or [], own)
            if res.get("done", True):
                break
            after = res["next_after"]
        pulled[kind] = {"new": added, "from": peer_node}
    return pulled


async def _push(client, own: str) -> dict:
    pushed = {}
    for kind in repo.KINDS:
        after, sent, added = 0, 0, 0
        while True:
            page = await asyncio.to_thread(export_page, kind, after, PAGE)
            for chunk in transfer.chunk_rows(page["rows"]):
                ack = await client.push_ml_rows(kind, own, chunk)
                if (ack or {}).get("error"):
                    raise MergeFailed(f"The VPS refused the {kind} rows: {ack['error']}")
                added += int(ack.get("inserted") or 0)
            sent += len(page["rows"])
            if page["done"]:
                break
            after = page["next_after"]
        pushed[kind] = {"sent": sent, "new": added}
    return pushed


async def run_merge(client) -> dict:
    """Both directions, then both benefit checks. Raises MergeFailed."""
    from backend.src.db import database as db_module
    own = await db_module.to_db_thread(own_node_id)
    _run["stage"] = "copying the VPS's rows here"
    pulled = await _pull(client, own)
    _run["stage"] = "copying this node's rows to the VPS"
    pushed = await _push(client, own)
    _run["stage"] = "checking the benefit on the VPS"
    try:
        ack = await client.request_ml_evaluate(timeout=600.0)
    except asyncio.TimeoutError as exc:
        raise MergeFailed("The VPS did not finish its check in time.") from exc
    if (ack or {}).get("error"):
        raise MergeFailed(f"The VPS's check failed: {ack['error']}")
    _run["stage"] = "checking the benefit here"
    local = await apply_async()
    return {"pulled": pulled, "pushed": pushed, "local": local,
            "remote": ack.get("result") or {}}


def status() -> dict:
    """The last run, and each dataset's current verdict on this node."""
    return {**_run, "datasets": {
        k: {"imported": repo.count(k), "decision": repo.decision(k)} for k in repo.KINDS}}


async def status_async() -> dict:
    return await asyncio.to_thread(status)


async def start_on_link() -> None:
    """`start` on this node's sync client: the Mac's link to its VPS."""
    from backend.src.services.cluster.sync import client as _client
    await start(_client.get_instance())


async def start(client) -> None:
    """Run the merge in the background; `status()` reports it. Refuses a second."""
    if _run["running"]:
        raise MergeFailed("A merge is already running.")
    _run.update(running=True, stage="starting", started_at=time.time(),
                finished_at=None, result=None, error=None)

    async def _go():
        try:
            _run["result"] = await run_merge(client)
        except Exception as exc:
            log.warning("[ml_merge] merge failed: %s", exc)
            _run["error"] = str(exc)
        finally:
            _run.update(running=False, stage="", finished_at=time.time())

    global _task
    _task = asyncio.get_running_loop().create_task(_go())


_task: Optional[asyncio.Task] = None  # held so the running merge is not collected
