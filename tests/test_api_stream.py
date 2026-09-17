"""Tests for the WS ``/events`` broadcast (002, T006-T016).

Crafted envelopes are published on the real singleton bus through the
TestClient's portal -- the same loop the broadcaster's consumers run on --
so ``websocket_connect`` drives the route fully in-process and every wait is
a deterministic state check, never a wall-clock guess.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from agency.api.app import create_app
from agency.api.config import ServiceConfig
from agency.api.stream import (
    CLOSE_GOING_AWAY,
    CLOSE_SLOW_CONSUMER,
    DEFAULT_QUEUE_MAXSIZE,
    EVENT_TYPES,
)
from agency.core.event_bus import get_event_bus, reset_event_bus
from agency.core.events import (
    EVENT_AGENT_DEQUEUED,
    EVENT_AGENT_QUEUED,
    EVENT_FALLBACK_ACTIVATED,
    EVENT_MODEL_OFFLOADED,
    EVENT_MODEL_RELOADED,
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_QUEUED,
    EVENT_NODE_RETRYING,
    EVENT_NODE_SKIPPED,
    EVENT_NODE_STARTED,
    EVENT_PHASE_COMPLETED,
    EVENT_PHASE_STARTED,
    EVENT_RUN_CANCEL,
    EVENT_SUMMARIZATION_TRIGGERED,
    EVENT_VRAM_FREED,
    EVENT_VRAM_THRESHOLD_EXCEEDED,
    AgentDequeued,
    AgentQueued,
    EventEnvelope,
    FallbackActivated,
    ModelOffloaded,
    ModelReloaded,
    NodeCompleted,
    NodeFailed,
    NodeQueued,
    NodeRetrying,
    NodeSkipped,
    NodeStarted,
    PhaseCompleted,
    PhaseStarted,
    RunCancelled,
    SummarizationTriggered,
    VRAMFreed,
    VRAMThresholdExceeded,
)

TWO_NODE_WORKFLOW = """\
name: app-two-node
entry_point: shout
nodes:
  shout:
    id: shout
    type: tool_call
    tool_name: upper
    arguments_template: '{"text": "hello"}'
  echo:
    id: echo
    type: tool_call
    tool_name: echo
    arguments_template: '{"text": "tail"}'
edges:
  - from_id: shout
    to_id: echo
"""

UNRESOLVED_WORKFLOW = """\
name: app-unresolved-demo
entry_point: a
nodes:
  a:
    id: a
    type: tool_call
    tool_name: upper
    arguments_template: '{"text": "{{ nodes.ghost.output }}"}'
"""

FIXED_TS = datetime(2026, 8, 27, 10, 0, 0, 123456, tzinfo=timezone.utc)

FIDELITY_CASES: list[tuple[str, Any]] = [
    (
        EVENT_VRAM_THRESHOLD_EXCEEDED,
        VRAMThresholdExceeded(
            used_bytes=13_743_895_347, limit_bytes=14_000_000_000, gpu_index=0
        ),
    ),
    (EVENT_VRAM_FREED, VRAMFreed(model_name="m", freed_bytes=1000, remaining_bytes=2000)),
    (
        EVENT_MODEL_OFFLOADED,
        ModelOffloaded(model_name="m", endpoint="http://127.0.0.1:8080", latency_ms=1.5),
    ),
    (
        EVENT_MODEL_RELOADED,
        ModelReloaded(model_name="m", endpoint="http://127.0.0.1:8080", latency_ms=2.5),
    ),
    (
        EVENT_AGENT_QUEUED,
        AgentQueued(node_id="n", model_name="m", queue_position=2, estimated_bytes=3000),
    ),
    (
        EVENT_AGENT_DEQUEUED,
        AgentDequeued(
            node_id="n", model_name="m", wait_seconds=1.25, queue_position_was=2
        ),
    ),
    (EVENT_PHASE_STARTED, PhaseStarted(phase_id="p", phase_name="P", run_id="r1")),
    (
        EVENT_PHASE_COMPLETED,
        PhaseCompleted(
            phase_id="p", phase_name="P", run_id="r1", completed_at=FIXED_TS
        ),
    ),
    (
        EVENT_SUMMARIZATION_TRIGGERED,
        SummarizationTriggered(phase_id="p", tokens_before=9000, tokens_after=400),
    ),
    (EVENT_NODE_QUEUED, NodeQueued(node_id="n", run_id="r1", queue_position=1)),
    (
        EVENT_NODE_STARTED,
        NodeStarted(node_id="n", run_id="r1", model="m", input="in", attempt=1),
    ),
    (
        EVENT_NODE_COMPLETED,
        NodeCompleted(
            node_id="n",
            run_id="r1",
            model="m",
            tokens_used=12,
            duration_seconds=0.25,
            attempt=1,
            output="OUT",
            fallback=None,
        ),
    ),
    (
        EVENT_NODE_FAILED,
        NodeFailed(
            node_id="n", run_id="r1", model="m", error="boom", stack_trace="tb"
        ),
    ),
    (
        EVENT_NODE_RETRYING,
        NodeRetrying(
            node_id="n",
            run_id="r1",
            model="m",
            error="boom",
            attempt=1,
            next_attempt=2,
            delay_seconds=0.5,
        ),
    ),
    (
        EVENT_NODE_SKIPPED,
        NodeSkipped(
            node_id="n",
            run_id="r1",
            reason="unresolved_binding",
            skipped_bindings=("nodes.ghost.output",),
        ),
    ),
    (
        EVENT_FALLBACK_ACTIVATED,
        FallbackActivated(
            node_id="n",
            run_id="r1",
            model="m",
            fallback="fb",
            error="boom",
            attempt=1,
        ),
    ),
    (EVENT_RUN_CANCEL, RunCancelled(run_id="r1")),
]


@pytest.fixture(autouse=True)
def _clean_bus():
    reset_event_bus()


@pytest.fixture
def log_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "runs"
    directory.mkdir()
    return directory


@pytest.fixture
def app(log_dir: Path):
    return create_app(ServiceConfig(log_dir=log_dir))


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


def publish(
    client: TestClient,
    event_type: str,
    seq: int,
    payload: Any,
    timestamp: datetime | None = None,
) -> EventEnvelope:
    envelope = EventEnvelope(
        event_type=event_type,
        seq=seq,
        timestamp=timestamp if timestamp is not None else datetime.now(timezone.utc),
        payload=payload,
    )
    client.portal.call(get_event_bus().publish, envelope)
    return envelope


def wire_value(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [wire_value(item) for item in value]
    if isinstance(value, list):
        return [wire_value(item) for item in value]
    if isinstance(value, dict):
        return {key: wire_value(item) for key, item in value.items()}
    return value


def wire_payload(payload: Any) -> Any:
    if dataclasses.is_dataclass(payload) and not isinstance(payload, type):
        payload = dataclasses.asdict(payload)
    return wire_value(payload)


def recv_json(ws) -> dict[str, Any]:
    message = ws.receive()
    assert message["type"] == "websocket.send", f"expected text frame, got {message}"
    return json.loads(message["text"])


def expect_close(ws, code: int) -> None:
    message = ws.receive()
    assert message["type"] == "websocket.close", f"expected close frame, got {message}"
    assert message["code"] == code, f"expected close {code}, got {message['code']}"


def wait_for(condition, *, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out waiting for condition")
        time.sleep(0.01)


# --- US1: live event stream --------------------------------------------------


class TestFidelity:
    def test_all_seventeen_kinds_roundtrip(self, client):
        with client.websocket_connect("/events") as ws:
            for seq, (event_type, payload) in enumerate(FIDELITY_CASES, start=1):
                publish(client, event_type, seq, payload, timestamp=FIXED_TS)
                frame = recv_json(ws)
                assert frame == {
                    "event_type": event_type,
                    "seq": seq,
                    "timestamp": FIXED_TS.isoformat(),
                    "run_id": getattr(payload, "run_id", None),
                    "payload": wire_payload(payload),
                }

    def test_large_output_untruncated(self, client):
        big = "x" * 100_000
        payload = NodeCompleted(
            node_id="n", run_id="r1", model=None, tokens_used=0, duration_seconds=0.0,
            output=big,
        )
        with client.websocket_connect("/events") as ws:
            publish(client, EVENT_NODE_COMPLETED, 1, payload)
            frame = recv_json(ws)
            assert frame["payload"]["output"] == big
            assert len(frame["payload"]["output"]) == 100_000


class TestOrdering:
    def test_gap_free_strictly_increasing_same_order(self, client):
        n = 20
        base = 100
        with client.websocket_connect("/events") as ws:
            for i in range(n):
                publish(
                    client,
                    EVENT_NODE_STARTED,
                    base + i,
                    NodeStarted(node_id=f"n{i}", run_id="r1", model=None),
                )
            frames = [recv_json(ws) for _ in range(n)]
        seqs = [frame["seq"] for frame in frames]
        assert seqs == list(range(base, base + n))
        assert [frame["payload"]["node_id"] for frame in frames] == [
            f"n{i}" for i in range(n)
        ]


class TestConnectLifecycle:
    def test_first_frame_is_first_event_no_fabrication(self, client):
        with client.websocket_connect("/events") as ws:
            publish(
                client,
                EVENT_NODE_STARTED,
                7,
                NodeStarted(node_id="n", run_id="r1", model=None),
            )
            frame = recv_json(ws)
            assert frame["event_type"] == EVENT_NODE_STARTED
            assert frame["seq"] == 7

    def test_unsolicited_client_data_ignored(self, client):
        with client.websocket_connect("/events") as ws:
            ws.send_text("hello server")
            ws.send_bytes(b"\x01\x02\x03")
            publish(
                client,
                EVENT_NODE_STARTED,
                8,
                NodeStarted(node_id="n", run_id="r1", model=None),
            )
            frame = recv_json(ws)
            assert frame["event_type"] == EVENT_NODE_STARTED
            assert frame["seq"] == 8

    def test_client_close_drops_connection_service_unaffected(self, client, app):
        stream = app.state.stream
        with client.websocket_connect("/events"):
            wait_for(lambda: len(stream.connections) == 1)
        wait_for(lambda: len(stream.connections) == 0)
        publish(
            client,
            EVENT_NODE_STARTED,
            1,
            NodeStarted(node_id="n", run_id="r1", model=None),
        )
        assert client.get("/health").status_code == 200


# --- US2: run lifecycle over the wire ----------------------------------------


class TestRunStreamE2E:
    def test_run_lifecycle_on_stream(self, client, tmp_path: Path):
        path = tmp_path / "two.yaml"
        path.write_text(TWO_NODE_WORKFLOW, encoding="utf-8")
        with client.websocket_connect("/events") as ws:
            run_id = client.post("/runs", json={"workflow": str(path)}).json()["run_id"]
            wait_for(
                lambda: client.get(f"/runs/{run_id}").json()["state"]
                in {"completed", "failed"}
            )
            frames = [recv_json(ws) for _ in range(6)]
        status = client.get(f"/runs/{run_id}").json()
        result = client.get(f"/runs/{run_id}/result").json()
        assert status["state"] == "completed"
        assert [frame["event_type"] for frame in frames] == [
            EVENT_NODE_QUEUED,
            EVENT_NODE_STARTED,
            EVENT_NODE_COMPLETED,
            EVENT_NODE_QUEUED,
            EVENT_NODE_STARTED,
            EVENT_NODE_COMPLETED,
        ]
        assert [frame["payload"]["node_id"] for frame in frames] == [
            "shout", "shout", "shout", "echo", "echo", "echo"
        ]
        assert all(
            frame["run_id"] == run_id == frame["payload"]["run_id"]
            for frame in frames
        )
        assert [frame["seq"] for frame in frames] == [1, 2, 3, 4, 5, 6]
        assert frames[2]["payload"]["output"] == "HELLO"
        assert frames[5]["payload"]["output"] == "tail"
        nodes = {node["node_id"]: node for node in result["nodes"]}
        assert nodes["shout"]["status"] == "completed"
        assert nodes["shout"]["output"] == "HELLO"
        assert nodes["echo"]["status"] == "completed"
        assert nodes["echo"]["output"] == "tail"

    def test_concurrent_runs_interleave_without_loss(self, client, tmp_path: Path):
        path = tmp_path / "two.yaml"
        path.write_text(TWO_NODE_WORKFLOW, encoding="utf-8")
        with client.websocket_connect("/events") as ws:
            id_a = client.post("/runs", json={"workflow": str(path)}).json()["run_id"]
            id_b = client.post("/runs", json={"workflow": str(path)}).json()["run_id"]
            wait_for(
                lambda: client.get(f"/runs/{id_a}").json()["state"]
                in {"completed", "failed"}
                and client.get(f"/runs/{id_b}").json()["state"]
                in {"completed", "failed"}
            )
            frames = [recv_json(ws) for _ in range(12)]
        by_run: dict[str, list[dict[str, Any]]] = {}
        for frame in frames:
            by_run.setdefault(frame["run_id"], []).append(frame)
        assert set(by_run) == {id_a, id_b}
        seqs = [frame["seq"] for frame in frames]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
        for run_id, run_frames in by_run.items():
            assert [frame["event_type"] for frame in run_frames] == [
                EVENT_NODE_QUEUED,
                EVENT_NODE_STARTED,
                EVENT_NODE_COMPLETED,
                EVENT_NODE_QUEUED,
                EVENT_NODE_STARTED,
                EVENT_NODE_COMPLETED,
            ]
            assert [frame["payload"]["node_id"] for frame in run_frames] == [
                "shout", "shout", "shout", "echo", "echo", "echo"
            ]
            assert all(frame["run_id"] == run_id for frame in run_frames)

    def test_unresolved_binding_skip_on_stream(self, client, tmp_path: Path):
        path = tmp_path / "unres.yaml"
        path.write_text(UNRESOLVED_WORKFLOW, encoding="utf-8")
        with client.websocket_connect("/events") as ws:
            run_id = client.post("/runs", json={"workflow": str(path)}).json()["run_id"]
            wait_for(
                lambda: client.get(f"/runs/{run_id}").json()["state"]
                in {"completed", "failed"}
            )
            frames = [recv_json(ws) for _ in range(3)]
        status = client.get(f"/runs/{run_id}").json()
        assert [frame["event_type"] for frame in frames] == [
            EVENT_NODE_QUEUED,
            EVENT_NODE_STARTED,
            EVENT_NODE_SKIPPED,
        ]
        assert all(frame["run_id"] == run_id for frame in frames)
        skipped = frames[2]["payload"]
        assert skipped["reason"] == "unresolved_binding"
        assert skipped["skipped_bindings"] == ["nodes.ghost.output"]
        assert status["nodes"]["a"]["status"] == "skipped"


# --- US3: connection robustness ----------------------------------------------


class TestMultiClient:
    def test_ten_clients_get_identical_ordered_frames(self, client, app):
        stream = app.state.stream
        sessions = []
        try:
            for _ in range(10):
                session = client.websocket_connect("/events")
                session.__enter__()
                sessions.append(session)
            wait_for(lambda: len(stream.connections) == 10)
            for i in range(5):
                publish(
                    client,
                    EVENT_NODE_STARTED,
                    i,
                    NodeStarted(node_id=f"n{i}", run_id="r1", model=None),
                )
            for session in sessions:
                frames = [recv_json(session) for _ in range(5)]
                assert [frame["seq"] for frame in frames] == [0, 1, 2, 3, 4]
                assert [frame["payload"]["node_id"] for frame in frames] == [
                    f"n{i}" for i in range(5)
                ]
        finally:
            for session in sessions:
                session.__exit__(None, None, None)


class TestClientDisconnect:
    def test_disconnected_client_dropped_others_unaffected(
        self, client, app, tmp_path: Path
    ):
        stream = app.state.stream
        survivor_a = client.websocket_connect("/events")
        survivor_a.__enter__()
        survivor_b = client.websocket_connect("/events")
        survivor_b.__enter__()
        try:
            gone = client.websocket_connect("/events")
            gone.__enter__()
            wait_for(lambda: len(stream.connections) == 3)
            gone.__exit__(None, None, None)
            wait_for(lambda: len(stream.connections) == 2)
            publish(
                client,
                EVENT_NODE_STARTED,
                1,
                NodeStarted(node_id="n", run_id="r1", model=None),
            )
            assert recv_json(survivor_a)["seq"] == 1
            assert recv_json(survivor_b)["seq"] == 1
            path = tmp_path / "two.yaml"
            path.write_text(TWO_NODE_WORKFLOW, encoding="utf-8")
            run_id = client.post(
                "/runs", json={"workflow": str(path)}
            ).json()["run_id"]
            wait_for(
                lambda: client.get(f"/runs/{run_id}").json()["state"]
                in {"completed", "failed"}
            )
            for session in (survivor_a, survivor_b):
                frames = [recv_json(session) for _ in range(6)]
                assert [frame["payload"]["node_id"] for frame in frames] == [
                    "shout", "shout", "shout", "echo", "echo", "echo"
                ]
        finally:
            for session in (survivor_a, survivor_b):
                session.__exit__(None, None, None)


class TestZeroClients:
    def test_publish_with_zero_clients_is_a_noop(self, client, app):
        stream = app.state.stream
        for i in range(3):
            publish(
                client,
                EVENT_NODE_STARTED,
                i,
                NodeStarted(node_id=f"n{i}", run_id="r1", model=None),
            )
        assert len(stream.connections) == 0
        with client.websocket_connect("/events") as ws:
            publish(
                client,
                EVENT_NODE_STARTED,
                99,
                NodeStarted(node_id="late", run_id="r1", model=None),
            )
            frame = recv_json(ws)
            assert frame["seq"] == 99
            assert frame["payload"]["node_id"] == "late"


class TestShutdown:
    def test_shutdown_closes_with_1001_and_unsubscribes(self, client, app):
        stream = app.state.stream
        with client.websocket_connect("/events") as ws:
            wait_for(lambda: len(stream.connections) == 1)
            conn = next(iter(stream.connections))
            client.portal.call(stream.stop)
            expect_close(ws, CLOSE_GOING_AWAY)
        assert conn.state == "closed"
        assert conn.close_code == CLOSE_GOING_AWAY
        assert conn.consumer.cancelled()
        assert not stream.running
        assert get_event_bus()._subscribers == {}

    def test_repeated_lifespan_cycles_leave_no_duplicate_handlers(self, log_dir):
        bus = get_event_bus()
        for _ in range(2):
            app = create_app(ServiceConfig(log_dir=log_dir))
            with TestClient(app):
                counts = {
                    event_type: len(bus._subscribers.get(event_type, ()))
                    for event_type in EVENT_TYPES
                }
                assert counts == dict.fromkeys(EVENT_TYPES, 1)
        assert bus._subscribers == {}


class TestSlowConsumer:
    def test_overflow_closes_slow_client_others_complete(self, client, app):
        stream = app.state.stream
        slow_session = client.websocket_connect("/events")
        slow_session.__enter__()
        fast_session = client.websocket_connect("/events")
        fast_session.__enter__()
        try:
            wait_for(lambda: len(stream.connections) == 2)
            slow = min(stream.connections, key=lambda c: c.id)
            started = asyncio.Event()

            async def blocked_send(*_args: Any, **_kwargs: Any) -> None:
                await started.wait()

            slow.websocket.send_text = blocked_send
            for i in range(DEFAULT_QUEUE_MAXSIZE + 2):
                publish(
                    client,
                    EVENT_NODE_STARTED,
                    i,
                    NodeStarted(node_id=f"n{i}", run_id="r1", model=None),
                )
            for i in range(DEFAULT_QUEUE_MAXSIZE + 2):
                frame = recv_json(fast_session)
                assert frame["seq"] == i
            wait_for(lambda: slow.state == "closed")
            wait_for(lambda: slow.consumer.cancelled())
            expect_close(slow_session, CLOSE_SLOW_CONSUMER)
            assert slow.close_code == CLOSE_SLOW_CONSUMER
        finally:
            for session in (slow_session, fast_session):
                session.__exit__(None, None, None)
