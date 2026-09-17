from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from agency.core.events import (
    EVENT_AGENT_DEQUEUED,
    EVENT_AGENT_QUEUED,
    AgentDequeued,
    AgentQueued,
)


class TestEventTypeConstants:
    def test_agent_queued_value(self):
        assert EVENT_AGENT_QUEUED == "agent_queued"

    def test_agent_dequeued_value(self):
        assert EVENT_AGENT_DEQUEUED == "agent_dequeued"


class TestAgentQueued:
    def test_fields_present(self):
        ev = AgentQueued(
            node_id="n1",
            model_name="m7b",
            queue_position=3,
            estimated_bytes=4096,
        )
        assert ev.node_id == "n1"
        assert ev.model_name == "m7b"
        assert ev.queue_position == 3
        assert ev.estimated_bytes == 4096

    def test_frozen(self):
        ev = AgentQueued(
            node_id="x", model_name="y", queue_position=1, estimated_bytes=100
        )
        with pytest.raises(FrozenInstanceError):
            ev.node_id = "z"


class TestAgentDequeued:
    def test_fields_present(self):
        ev = AgentDequeued(
            node_id="n2",
            model_name="m7b",
            wait_seconds=1.5,
            queue_position_was=2,
        )
        assert ev.node_id == "n2"
        assert ev.model_name == "m7b"
        assert ev.wait_seconds == 1.5
        assert ev.queue_position_was == 2

    def test_frozen(self):
        ev = AgentDequeued(
            node_id="a", model_name="b", wait_seconds=0.1, queue_position_was=1
        )
        with pytest.raises(FrozenInstanceError):
            ev.wait_seconds = 99.9
