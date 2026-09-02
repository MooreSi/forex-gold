"""Stand down, resume, and moving the models across.

`request_stand_down`'s docstring carries the rule these exist for: the caller
-- the UI's Local/Remote toggle -- **must not flip to Local mode without the
ack, or the mutual-exclusion guarantee is void.** Two nodes both believing they
are the active trader is the worst state this cluster has, because both will
open on the same signal.

So the ack is not a nicety and a timeout is not a shrug: it raises, and the
toggle stays where it was.

`request_model_snapshot` is the other side -- it moves trained models between
the two machines, and an upload that half-completes leaves the VPS scoring on
a partial set.

No socket: the websocket is a list, and the model packager is faked.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster.sync import client as sc
from backend.src.services.cluster.sync.protocol import (
    MSG_MODEL_SNAPSHOT_END, MSG_MODEL_SNAPSHOT_REQUEST, MSG_MODEL_SNAPSHOT_UPLOAD,
    MSG_RESUME, MSG_STAND_DOWN,
)

pytestmark = pytest.mark.asyncio


class _Ws:
    def __init__(self):
        self.sent: list = []
        self.binary: list = []

    async def send(self, raw):
        if isinstance(raw, (bytes, bytearray)):
            self.binary.append(bytes(raw))
        else:
            self.sent.append(json.loads(raw))

    def types(self):
        return [m.get("type") for m in self.sent]


@pytest.fixture
def node():
    cli = sc.SyncClient.__new__(sc.SyncClient)
    cli.conn_state = sc.CONN_CONNECTED
    cli._ws = _Ws()
    cli._stand_down_ack_event = asyncio.Event()
    cli._resume_ack_event = asyncio.Event()
    cli._model_download_event = asyncio.Event()
    cli._last_stand_down_ack = {}
    return cli


async def _ack(node, event, attr=None, payload=None, delay=0.02):
    await asyncio.sleep(delay)
    if attr:
        setattr(node, attr, payload)
    getattr(node, event).set()


class TestStandingDown:
    async def test_it_returns_the_vps_summary(self, node):
        task = asyncio.create_task(node.request_stand_down(timeout=1.0))
        asyncio.create_task(_ack(node, "_stand_down_ack_event",
                                 "_last_stand_down_ack", {"open_positions": 2}))

        assert (await task) == {"open_positions": 2}

    async def test_it_sends_the_stand_down(self, node):
        task = asyncio.create_task(node.request_stand_down(timeout=1.0))
        asyncio.create_task(_ack(node, "_stand_down_ack_event",
                                 "_last_stand_down_ack", {}))
        await task

        assert MSG_STAND_DOWN in node._ws.types()

    async def test_a_timeout_raises(self, node):
        """The rule from the docstring. Returning quietly would let the UI
        flip to Local while the VPS is still trading -- both nodes active on
        the same signal."""
        with pytest.raises(asyncio.TimeoutError):
            await node.request_stand_down(timeout=0.05)

    async def test_a_stale_ack_does_not_answer_a_new_request(self, node):
        """The event is long-lived. Left set from a previous stand-down, the
        toggle would flip on an ack the VPS never sent for this request."""
        node._last_stand_down_ack = {"from": "an earlier request"}
        node._stand_down_ack_event.set()

        task = asyncio.create_task(node.request_stand_down(timeout=0.5))
        await asyncio.sleep(0.05)

        assert not task.done()
        await _ack(node, "_stand_down_ack_event", "_last_stand_down_ack",
                   {"from": "this one"})
        assert (await task)["from"] == "this one"

    async def test_disconnected_it_refuses(self, node):
        node.conn_state = sc.CONN_DISCONNECTED

        with pytest.raises(ConnectionError):
            await node.request_stand_down()

    async def test_no_socket_it_refuses(self, node):
        node._ws = None

        with pytest.raises(ConnectionError):
            await node.request_stand_down()


class TestResuming:
    async def test_it_sends_and_waits(self, node):
        task = asyncio.create_task(node.request_resume(timeout=1.0))
        asyncio.create_task(_ack(node, "_resume_ack_event"))
        await task

        assert MSG_RESUME in node._ws.types()

    async def test_a_timeout_raises(self, node):
        with pytest.raises(asyncio.TimeoutError):
            await node.request_resume(timeout=0.05)

    async def test_a_stale_ack_does_not_answer_it(self, node):
        node._resume_ack_event.set()

        task = asyncio.create_task(node.request_resume(timeout=0.4))
        await asyncio.sleep(0.05)

        assert not task.done()
        await _ack(node, "_resume_ack_event")
        await task

    async def test_disconnected_it_refuses(self, node):
        node.conn_state = sc.CONN_DISCONNECTED

        with pytest.raises(ConnectionError):
            await node.request_resume()


class TestMovingTheModels:
    async def test_a_download_asks_and_waits(self, node):
        task = asyncio.create_task(node.request_model_snapshot("download", timeout=1.0))
        asyncio.create_task(_ack(node, "_model_download_event"))
        await task

        assert MSG_MODEL_SNAPSHOT_REQUEST in node._ws.types()

    async def test_a_download_that_never_arrives_raises(self, node):
        """Silence must not read as "downloaded". The caller would then
        believe this node is scoring on the VPS's models."""
        with pytest.raises(asyncio.TimeoutError):
            await node.request_model_snapshot("download", timeout=0.05)

    async def test_an_upload_frames_the_bytes_between_a_header_and_an_end(
        self, node, monkeypatch,
    ):
        """The receiver reassembles by those two markers. Without the end
        marker it holds a partial archive open for ever."""
        monkeypatch.setattr(
            "backend.src.services.cluster.sync.model_transfer.package_models",
            lambda d: (b"x" * 100, ["a.pkl"]))
        monkeypatch.setattr(
            "backend.src.services.cluster.sync.model_transfer.data_dir", lambda: "/tmp")

        await node.request_model_snapshot("upload")

        assert node._ws.types() == [MSG_MODEL_SNAPSHOT_UPLOAD, MSG_MODEL_SNAPSHOT_END]

    async def test_the_whole_archive_is_sent(self, node, monkeypatch):
        """Chunked at 32KB. A payload larger than one chunk must arrive
        complete, or the VPS unpacks a truncated archive."""
        payload = bytes(range(256)) * 400          # ~100KB, four chunks
        monkeypatch.setattr(
            "backend.src.services.cluster.sync.model_transfer.package_models",
            lambda d: (payload, ["a.pkl"]))
        monkeypatch.setattr(
            "backend.src.services.cluster.sync.model_transfer.data_dir", lambda: "/tmp")

        await node.request_model_snapshot("upload")

        assert len(node._ws.binary) > 1
        assert b"".join(node._ws.binary) == payload

    async def test_the_header_declares_the_real_size(self, node, monkeypatch):
        monkeypatch.setattr(
            "backend.src.services.cluster.sync.model_transfer.package_models",
            lambda d: (b"y" * 4242, ["a.pkl", "b.pkl"]))
        monkeypatch.setattr(
            "backend.src.services.cluster.sync.model_transfer.data_dir", lambda: "/tmp")

        await node.request_model_snapshot("upload")

        header = node._ws.sent[0]
        assert header["size"] == 4242
        assert header["files"] == ["a.pkl", "b.pkl"]

    async def test_a_stale_download_event_does_not_answer_a_new_request(self, node):
        """Same long-lived-event trap as the stand-down ack: left set by an
        earlier download, the next request returns instantly and the caller
        believes the VPS's models arrived when nothing was sent.

        Mutation found this gap -- deleting the clear() changed no test,
        because the other two download cases work whether or not it runs.
        """
        node._model_download_event.set()

        task = asyncio.create_task(node.request_model_snapshot("download", timeout=0.4))
        await asyncio.sleep(0.05)

        assert not task.done(), "returned on a stale download event"

        await _ack(node, "_model_download_event")
        await task

    async def test_an_unknown_direction_is_an_error(self, node):
        """Not a silent no-op: the caller believes models moved."""
        with pytest.raises(ValueError):
            await node.request_model_snapshot("sideways")

    async def test_disconnected_it_refuses(self, node):
        node.conn_state = sc.CONN_DISCONNECTED

        with pytest.raises(ConnectionError):
            await node.request_model_snapshot("download")
