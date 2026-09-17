from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agency.core.events import (
    EVENT_NODE_COMPLETED,
    EVENT_NODE_FAILED,
    EVENT_NODE_STARTED,
    EVENT_VRAM_FREED,
    EVENT_VRAM_THRESHOLD_EXCEEDED,
    EventEnvelope,
    NodeCompleted,
    NodeFailed,
    NodeStarted,
    VRAMFreed,
    VRAMThresholdExceeded,
)


class TestEventTypeConstants:
    def test_vram_threshold_exceeded_value(self):
        assert EVENT_VRAM_THRESHOLD_EXCEEDED == "vram_threshold_exceeded"

    def test_vram_freed_value(self):
        assert EVENT_VRAM_FREED == "vram_freed"


class TestVRAMThresholdExceeded:
    def test_fields_present(self):
        ev = VRAMThresholdExceeded(used_bytes=1024, limit_bytes=2048, gpu_index=0)
        assert ev.used_bytes == 1024
        assert ev.limit_bytes == 2048
        assert ev.gpu_index == 0

    def test_frozen(self):
        ev = VRAMThresholdExceeded(used_bytes=100, limit_bytes=200, gpu_index=1)
        with pytest.raises(FrozenInstanceError):
            ev.used_bytes = 999


class TestVRAMFreed:
    def test_fields_present(self):
        ev = VRAMFreed(model_name="llama-7b", freed_bytes=4096, remaining_bytes=2048)
        assert ev.model_name == "llama-7b"
        assert ev.freed_bytes == 4096
        assert ev.remaining_bytes == 2048

    def test_frozen(self):
        ev = VRAMFreed(model_name="m", freed_bytes=100, remaining_bytes=50)
        with pytest.raises(FrozenInstanceError):
            ev.model_name = "other"


class TestEventEnvelope:
    def test_fields_present(self):
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        env = EventEnvelope(
            event_type="test", seq=42, timestamp=now, payload={"k": "v"}
        )
        assert env.event_type == "test"
        assert env.seq == 42
        assert env.timestamp == now
        assert env.payload == {"k": "v"}

    def test_frozen(self):
        from datetime import datetime, timezone

        env = EventEnvelope(
            event_type="t", seq=1, timestamp=datetime.now(timezone.utc), payload={}
        )
        with pytest.raises(FrozenInstanceError):
            env.seq = 999

    def test_create_classmethod(self):
        env = EventEnvelope.create("my_event", 7, {"data": True})
        assert env.event_type == "my_event"
        assert env.seq == 7
        assert env.payload == {"data": True}
        assert env.timestamp.tzinfo is not None


class TestNodeEventTypeConstants:
    def test_node_started_value(self):
        assert EVENT_NODE_STARTED == "node_started"

    def test_node_completed_value(self):
        assert EVENT_NODE_COMPLETED == "node_completed"

    def test_node_failed_value(self):
        assert EVENT_NODE_FAILED == "node_failed"


class TestNodeStarted:
    def test_fields_present(self):
        ev = NodeStarted(node_id="n1", run_id="r1", model="llama-7b")
        assert ev.node_id == "n1"
        assert ev.run_id == "r1"
        assert ev.model == "llama-7b"

    def test_model_optional_for_non_agent_nodes(self):
        ev = NodeStarted(node_id="n1", run_id="r1", model=None)
        assert ev.model is None

    def test_frozen(self):
        ev = NodeStarted(node_id="n1", run_id="r1", model="m")
        with pytest.raises(FrozenInstanceError):
            ev.node_id = "other"


class TestNodeCompleted:
    def test_fields_present(self):
        ev = NodeCompleted(
            node_id="n1",
            run_id="r1",
            model="llama-7b",
            tokens_used=42,
            duration_seconds=1.5,
        )
        assert ev.node_id == "n1"
        assert ev.run_id == "r1"
        assert ev.model == "llama-7b"
        assert ev.tokens_used == 42
        assert ev.duration_seconds == 1.5

    def test_model_optional_for_non_agent_nodes(self):
        ev = NodeCompleted(
            node_id="n1",
            run_id="r1",
            model=None,
            tokens_used=10,
            duration_seconds=0.25,
        )
        assert ev.model is None

    def test_frozen(self):
        ev = NodeCompleted(
            node_id="n1", run_id="r1", model="m", tokens_used=1, duration_seconds=0.1
        )
        with pytest.raises(FrozenInstanceError):
            ev.tokens_used = 999


class TestNodeFailed:
    def test_fields_present(self):
        ev = NodeFailed(node_id="n1", run_id="r1", model=None, error="boom")
        assert ev.node_id == "n1"
        assert ev.run_id == "r1"
        assert ev.model is None
        assert ev.error == "boom"

    def test_frozen(self):
        ev = NodeFailed(node_id="n1", run_id="r1", model="m", error="boom")
        with pytest.raises(FrozenInstanceError):
            ev.error = "other"
